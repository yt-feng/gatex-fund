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
    def __init__(
        self,
        message: str,
        *,
        category: str = "delivery",
        http_status: int | None = None,
        cause_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.http_status = http_status
        self.cause_type = cause_type

    def diagnostic_fields(self) -> tuple[str, ...]:
        fields = (f"category={self.category}",)
        if self.http_status is not None:
            fields += (f"http_status={self.http_status}",)
        if self.cause_type:
            fields += (f"cause_type={self.cause_type}",)
        return fields


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def build_direct_opener() -> urllib.request.OpenerDirector:
    # Collection may require an egress proxy. GateX delivery carries a bearer
    # credential and must never inherit that collector-only proxy environment.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        RejectRedirects(),
    )


_DIRECT_NO_REDIRECT_OPENER = build_direct_opener()


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
        raise IntakeDeliveryError(
            "intake endpoint is not allowed",
            category="configuration",
        )
    return f"https://{parsed.hostname}{_ENDPOINT_PATH}"


def validate_delivery_configuration(*, endpoint: str, token: str) -> str:
    target = validate_endpoint(endpoint)
    bearer = token.strip()
    if len(bearer) < 24 or any(character.isspace() for character in bearer):
        raise IntakeDeliveryError(
            "intake credential is unavailable",
            category="configuration",
        )
    return target


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise IntakeDeliveryError(
            "intake JSONL is unavailable",
            category="input",
            cause_type=type(error).__name__,
        ) from error
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise IntakeDeliveryError(
                "intake JSONL is invalid",
                category="input",
                cause_type=type(error).__name__,
            ) from error
        if not isinstance(value, dict):
            raise IntakeContractError("intake envelope schema is invalid")
        validate_envelope(value)
        values.append(value)
    return values


def _default_open(request: urllib.request.Request, timeout: float):
    return _DIRECT_NO_REDIRECT_OPENER.open(request, timeout=timeout)


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
        payload = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
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
        last_error: IntakeDeliveryError | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response = opener(request, timeout)
                status_value = getattr(response, "status", None)
                status = int(
                    status_value
                    if status_value is not None
                    else response.getcode()
                )
                if 200 <= status < 300:
                    delivered += 1
                    last_error = None
                    break
                if status < 500:
                    raise IntakeDeliveryError(
                        f"intake rejected the envelope with HTTP {status}",
                        category="http-rejected",
                        http_status=status,
                    )
                last_error = IntakeDeliveryError(
                    f"intake returned HTTP {status}",
                    category="http-retryable",
                    http_status=status,
                    cause_type="HTTPResponse",
                )
            except urllib.error.HTTPError as error:
                if error.code < 500:
                    raise IntakeDeliveryError(
                        f"intake rejected the envelope with HTTP {error.code}",
                        category="http-rejected",
                        http_status=error.code,
                        cause_type=type(error).__name__,
                    ) from error
                last_error = IntakeDeliveryError(
                    f"intake returned HTTP {error.code}",
                    category="http-retryable",
                    http_status=error.code,
                    cause_type=type(error).__name__,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = IntakeDeliveryError(
                    "intake transport did not complete",
                    category="transport",
                    cause_type=type(error).__name__,
                )
            if attempt < max(1, attempts):
                time.sleep(min(4.0, float(2 ** (attempt - 1))))
        if last_error is not None:
            raise IntakeDeliveryError(
                "intake delivery did not complete",
                category=last_error.category,
                http_status=last_error.http_status,
                cause_type=last_error.cause_type,
            ) from last_error
    return delivered


def deliver_file(path: Path, *, mode: str, endpoint: str = "") -> int:
    envelopes = load_jsonl(path)
    if mode == "dry-run":
        return len(envelopes)
    if mode != "post":
        raise IntakeDeliveryError(
            "delivery mode must be dry-run or post",
            category="configuration",
        )
    token = os.environ.pop("GATEX_INTELLIGENCE_INTAKE_SECRET", "")
    return deliver_envelopes(envelopes, endpoint=endpoint, token=token)
