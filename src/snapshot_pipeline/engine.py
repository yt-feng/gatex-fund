from __future__ import annotations

import contextlib
import copy
import importlib.util
import os
import re
import secrets
import signal
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, TextIO

from .io import atomic_write_json, load_json


_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SAFE_STAGES = {"discover", "detail", "assets", "state", "run"}
_SAFE_STATUSES = {"start", "ok", "empty", "retry", "failed"}
_DISCOVERY_RETRY_DELAYS = (30, 90)
_DISCOVERY_TIMEOUT_SECONDS = 180


class _DiscoveryTimeout(BaseException):
    # Provider exception handlers must not turn the deadline into another
    # unbounded internal retry. The runner handles it at the attempt boundary.
    status = "timeout"


@contextlib.contextmanager
def _discovery_deadline():
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expire(_signum, _frame):
        raise _DiscoveryTimeout("source discovery exceeded its time limit")

    def cancel():
        signal.setitimer(signal.ITIMER_REAL, 0)

    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, _DISCOVERY_TIMEOUT_SECONDS)
    try:
        yield cancel
    finally:
        cancel()
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            remaining = max(0.000001, previous_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


def _load_overlay(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_sealed_provider_overlay", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("provider overlay is not loadable")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
        raise
    return module


def _safe_logger(stream: TextIO) -> Callable[..., None]:
    def emit(*, stage: str, status: str, count: int | None = None) -> None:
        if stage not in _SAFE_STAGES or status not in _SAFE_STATUSES:
            raise ValueError("provider emitted an invalid log event")
        fields = [f"stage={stage}", f"status={status}"]
        if count is not None:
            fields.append(f"count={int(count)}")
        print(" ".join(fields), file=stream, flush=True)

    return emit


def _validate_result(result: Any, batch_dir: Path) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError("provider returned an invalid result")
    state = result.get("state")
    new_count = result.get("new_count")
    discovered_count = result.get("discovered_count")
    deferred_count = result.get("deferred_count", 0)
    if not isinstance(state, dict):
        raise RuntimeError("provider returned an invalid state")
    if type(new_count) is not int or new_count < 0:
        raise RuntimeError("provider returned an invalid new-item count")
    if type(discovered_count) is not int or discovered_count < new_count:
        raise RuntimeError("provider returned an invalid discovery count")
    if type(deferred_count) is not int or deferred_count < 0:
        raise RuntimeError("provider returned an invalid deferred count")
    failure_class = result.get("failure_class", "captcha")
    verified_partial = failure_class == 'upstream' and result.get('upstream_verified') is True
    if deferred_count and not new_count and not verified_partial:
        raise RuntimeError("provider deferred collection without completed new items")
    if deferred_count and (not isinstance(failure_class, str) or failure_class not in {"captcha", "upstream"}):
        raise RuntimeError("provider returned an invalid deferred failure class")
    if new_count and not batch_dir.is_dir():
        raise RuntimeError("provider did not materialize the batch")
    return {
        "state": state,
        "new_count": new_count,
        "discovered_count": discovered_count,
        "deferred_count": deferred_count,
        "failure_class": failure_class if deferred_count else "",
    }


def run_pipeline(
    *,
    config_path: Path,
    provider_path: Path,
    state_path: Path,
    state_out: Path,
    work_dir: Path,
    result_path: Path,
) -> int:
    work_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(work_dir, 0o700)
    config = load_json(config_path)
    state = load_json(state_path)
    if not isinstance(config, dict) or not isinstance(state, dict):
        raise RuntimeError("runtime payload is invalid")
    original_state = copy.deepcopy(state)
    token_env = config.get("token_env")
    token: str | None = None
    if token_env is not None:
        if not isinstance(token_env, str) or not _ENV_NAME.fullmatch(token_env):
            raise RuntimeError("runtime token binding is invalid")
        token = os.environ.get(token_env, "")
        if not token:
            raise RuntimeError("runtime credential is unavailable")
        os.environ.pop(token_env, None)

    batch_dir = work_dir / "batch"
    private_output_path = work_dir / "provider-output.log"
    with private_output_path.open("w", encoding="utf-8") as private_output:
        os.chmod(private_output_path, 0o600)
        safe_logger = _safe_logger(sys.stdout)
        with contextlib.redirect_stdout(private_output), contextlib.redirect_stderr(private_output):
            for attempt in range(len(_DISCOVERY_RETRY_DELAYS) + 1):
                stages: set[str] = set()
                discovery_finished = False

                def emit(*, stage: str, status: str, count: int | None = None) -> None:
                    nonlocal discovery_finished
                    safe_logger(stage=stage, status=status, count=count)
                    stages.add(stage)
                    if stage == "discover" and status in {"ok", "empty"}:
                        discovery_finished = True
                    if discovery_finished or stage in {"detail", "assets", "state"}:
                        cancel_discovery_deadline()

                overlay = _load_overlay(provider_path)
                collect = getattr(overlay, "collect", None)
                if not callable(collect):
                    raise RuntimeError("provider overlay has no collector")
                try:
                    with _discovery_deadline() as cancel_discovery_deadline:
                        provider_result = collect(
                            config=copy.deepcopy(config),
                            state=copy.deepcopy(original_state),
                            token=token,
                            output=batch_dir,
                            emit=emit,
                        )
                    break
                except (Exception, _DiscoveryTimeout) as error:
                    # Discovery has not produced a publishable batch. Retry
                    # only this typed transient block, never detail, asset,
                    # validation, delivery, or persistence failures.
                    if (
                        (getattr(error, "status", None) != "captcha" and not isinstance(error, _DiscoveryTimeout))
                        or "discover" not in stages
                        or discovery_finished
                        or stages - {"discover", "run"}
                        or attempt >= len(_DISCOVERY_RETRY_DELAYS)
                    ):
                        raise
                    # Keep failed bytes inside the sealed diagnostic area,
                    # and give the next attempt a fresh output and state.
                    if batch_dir.exists() or batch_dir.is_symlink():
                        batch_dir.rename(work_dir / f"failed-attempt-{attempt + 1}")
                    safe_logger(stage="discover", status="retry", count=attempt + 1)
                    time.sleep(_DISCOVERY_RETRY_DELAYS[attempt])
    result = _validate_result(provider_result, batch_dir)
    atomic_write_json(state_out, result["state"])
    public_result = {
        "new_count": result["new_count"],
        "discovered_count": result["discovered_count"],
        "deferred_count": result["deferred_count"],
        "status": "partial" if result["deferred_count"] else "ok",
        "state_changed": result["state"] != original_state,
        "batch_path": str(batch_dir) if result["new_count"] else "",
    }
    if result["deferred_count"]:
        public_result["failure_class"] = result["failure_class"]
    atomic_write_json(result_path, public_result)
    deferred_log = (
        f" deferred_count={result['deferred_count']} failure_class={result['failure_class']}"
        if result["deferred_count"] else ""
    )
    print(
        f"stage=run status={public_result['status']} count={result['new_count']}{deferred_log}",
        flush=True,
    )
    return result["new_count"]


def private_failure(work_dir: Path, error: BaseException) -> str:
    error_id = secrets.token_hex(6)
    try:
        atomic_write_json(
            work_dir / "private-error.json",
            {"error_id": error_id, "type": type(error).__name__, "message": str(error)},
        )
    except Exception:
        pass
    return error_id
