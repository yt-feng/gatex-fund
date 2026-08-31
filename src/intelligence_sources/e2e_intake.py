from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Mapping

from .contract import (
    build_official_report_e2e_envelope,
    validate_envelope as validate_shared_envelope,
)


INTAKE_ENDPOINT = "https://gatex.fund/api/integrations/intelligence/intake"


class IntakeInputError(ValueError):
    """An intentionally non-sensitive input or configuration failure."""


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def build_direct_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        RejectRedirects(),
    )


def build_envelope() -> dict[str, Any]:
    envelope = build_official_report_e2e_envelope()
    validate_envelope(envelope)
    return envelope


def validate_envelope(envelope: Mapping[str, Any]) -> None:
    # The controlled workflow has no payload inputs. The shared collector
    # contract is the final authority for its exact IEA identity and policy.
    validate_shared_envelope(envelope)


def validate_delivery_configuration(*, endpoint: str, token: str) -> None:
    if endpoint != INTAKE_ENDPOINT:
        raise IntakeInputError("endpoint is invalid")
    if len(token) < 24 or any(character.isspace() for character in token):
        raise IntakeInputError("credential is invalid")


def post_envelope(
    envelope: Mapping[str, Any],
    *,
    endpoint: str,
    token: str,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: float = 30.0,
) -> int:
    validate_delivery_configuration(endpoint=endpoint, token=token)
    validate_envelope(envelope)
    payload = json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": str(envelope["idempotencyKey"]),
            "User-Agent": "gatex-curated-source-e2e/1",
        },
    )
    direct_opener = opener or build_direct_opener()
    try:
        with direct_opener.open(request, timeout=timeout) as response:
            status = int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    return status


def main() -> int:
    try:
        token = os.environ.pop("GATEX_INTELLIGENCE_INTAKE_SECRET", "")
        endpoint = os.environ.get("GATEX_INTELLIGENCE_INTAKE_URL", "")
        status = post_envelope(build_envelope(), endpoint=endpoint, token=token)
    except Exception:
        print("GateX intake HTTP status: unavailable")
        return 1
    print(f"GateX intake HTTP status: {status}")
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
