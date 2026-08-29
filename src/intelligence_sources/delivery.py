from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse

from .contract import IntakeContractError, validate_envelope


_ALLOWED_ENDPOINT_HOSTS = {"gatex.fund"}
_ENDPOINT_PATH = "/api/integrations/intelligence/intake"


class IntakeDeliveryError(RuntimeError):
    pass


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(RejectRedirects())


def validate_endpoint(value: str) -> str:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_ENDPOINT_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.path != _ENDPOINT_PATH
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise IntakeDeliveryError("intake endpoint is not allowed")
    return f"https://{parsed.hostname}{_ENDPOINT_PATH}"


def validate_delivery_configuration(*, endpoint: str, token: str) -> str:
    target = validate_endpoint(endpoint)
    bearer = token.strip()
    if len(bearer) < 24 or any(character.isspace() for character in bearer):
        raise IntakeDeliveryError("intake credential is unavailable")
    return target


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise IntakeDeliveryError("intake JSONL is unavailable") from error
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise IntakeDeliveryError("intake JSONL is invalid") from error
        if not isinstance(value, dict):
            raise IntakeContractError("intake envelope schema is invalid")
        validate_envelope(value)
        values.append(value)
    return values


def _default_open(request: urllib.request.Request, timeout: float):
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def deliver_envelopes(
    envelopes: Iterable[Mapping[str, Any]],
    *,
    endpoint: str,
    token: str,
    timeout: float = 30.0,
    attempts: int = 3,
    opener: Callable[[urllib.request.Request, float], Any] = _default_open,
) -> int:
    target = validate_delivery_configuration(endpoint=endpoint, token=token)
    bearer = token.strip()
    delivered = 0
    for envelope in envelopes:
        validate_envelope(envelope)
        idempotency_key = str(envelope.get("idempotencyKey") or "")
        if len(idempotency_key) != 64:
            raise IntakeContractError("intake idempotency key is invalid")
        payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            target,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
                "User-Agent": "gatex-intelligence-source-runner/1",
            },
        )
        last_error: BaseException | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response = opener(request, timeout)
                status_value = getattr(response, "status", None)
                status = int(status_value if status_value is not None else response.getcode())
                if 200 <= status < 300:
                    delivered += 1
                    last_error = None
                    break
                if status < 500:
                    raise IntakeDeliveryError(f"intake rejected the envelope with HTTP {status}")
                last_error = IntakeDeliveryError(f"intake returned HTTP {status}")
            except urllib.error.HTTPError as error:
                if error.code < 500:
                    raise IntakeDeliveryError(
                        f"intake rejected the envelope with HTTP {error.code}"
                    ) from error
                last_error = error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
            if attempt < max(1, attempts):
                time.sleep(min(4.0, float(2 ** (attempt - 1))))
        if last_error is not None:
            raise IntakeDeliveryError("intake delivery did not complete") from last_error
    return delivered


def deliver_file(path: Path, *, mode: str, endpoint: str = "") -> int:
    envelopes = load_jsonl(path)
    if mode == "dry-run":
        return len(envelopes)
    if mode != "post":
        raise IntakeDeliveryError("delivery mode must be dry-run or post")
    token = os.environ.pop("GATEX_INTELLIGENCE_INTAKE_SECRET", "")
    return deliver_envelopes(envelopes, endpoint=endpoint, token=token)
