from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import secrets
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, TextIO

from .io import atomic_write_json, load_json


_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SAFE_STAGES = {"discover", "detail", "assets", "state", "run"}
_SAFE_STATUSES = {"start", "ok", "empty", "retry", "failed"}


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
    if not isinstance(state, dict):
        raise RuntimeError("provider returned an invalid state")
    if not isinstance(new_count, int) or new_count < 0:
        raise RuntimeError("provider returned an invalid new-item count")
    if not isinstance(discovered_count, int) or discovered_count < new_count:
        raise RuntimeError("provider returned an invalid discovery count")
    if new_count and not batch_dir.is_dir():
        raise RuntimeError("provider did not materialize the batch")
    return {
        "state": state,
        "new_count": new_count,
        "discovered_count": discovered_count,
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
            overlay = _load_overlay(provider_path)
            collect = getattr(overlay, "collect", None)
            if not callable(collect):
                raise RuntimeError("provider overlay has no collector")
            provider_result = collect(
                config=config,
                state=state,
                token=token,
                output=batch_dir,
                emit=safe_logger,
            )
    result = _validate_result(provider_result, batch_dir)
    atomic_write_json(state_out, result["state"])
    public_result = {
        "new_count": result["new_count"],
        "discovered_count": result["discovered_count"],
        "state_changed": result["state"] != state,
        "batch_path": str(batch_dir) if result["new_count"] else "",
    }
    atomic_write_json(result_path, public_result)
    print(
        f"stage=run status=ok count={result['new_count']}",
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
