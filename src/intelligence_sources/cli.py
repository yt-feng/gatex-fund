from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from snapshot_pipeline.io import load_json

from .contract import envelopes_from_batch, write_jsonl
from .delivery import (
    IntakeDeliveryError,
    deliver_file,
    validate_delivery_configuration,
)
from .tikhub_backfill import TikHubTransport, run_backfill_page, verify_profile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gatex-intelligence-sources")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export-batch")
    export.add_argument("--batch", type=Path, required=True)
    export.add_argument("--config", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)

    deliver = subparsers.add_parser("deliver")
    deliver.add_argument("--input", type=Path, required=True)
    deliver.add_argument("--mode", choices=("dry-run", "post"), required=True)
    deliver.add_argument("--endpoint", default="")

    delivery_check = subparsers.add_parser("check-delivery")
    delivery_check.add_argument("--endpoint", required=True)

    inspect = subparsers.add_parser("inspect-config")
    inspect.add_argument("--config", type=Path, required=True)

    backfill = subparsers.add_parser("backfill-page")
    backfill.add_argument("--config", type=Path, required=True)
    backfill.add_argument("--state", type=Path, required=True)
    backfill.add_argument("--state-out", type=Path, required=True)
    backfill.add_argument("--output", type=Path, required=True)
    backfill.add_argument("--maximum-items", type=int, default=10)
    backfill.add_argument("--base-url", default="https://api.tikhub.io")

    profile = subparsers.add_parser("verify-profile")
    profile.add_argument("--config", type=Path, required=True)
    profile.add_argument("--base-url", default="https://api.tikhub.io")
    return parser


def _intake_config(path: Path) -> dict:
    value = load_json(path)
    intake = value.get("intelligence_intake") if isinstance(value, dict) else None
    if not isinstance(intake, dict):
        raise RuntimeError("sealed profile has no intelligence intake configuration")
    if intake.get("enabled") is not True:
        raise RuntimeError("sealed profile is disabled")
    if intake.get("verification_status") != "verified":
        raise RuntimeError("sealed profile identity is not verified")
    for key in ("channel_key", "publisher", "author"):
        value = intake.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"sealed profile {key} is unavailable")
    if intake.get("language") not in {"zh", "en"}:
        raise RuntimeError("sealed profile language is incompatible with GateX")
    if intake.get("access_scope") not in {"public", "member", "advanced", "staff"}:
        raise RuntimeError("sealed profile access scope is incompatible with GateX")
    return intake


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "export-batch":
            envelopes = envelopes_from_batch(
                batch_root=args.batch,
                intake_config=_intake_config(args.config),
            )
            count = write_jsonl(args.output, envelopes)
            print(f"stage=intake-export status=ok count={count}")
            return 0
        if args.command == "deliver":
            count = deliver_file(args.input, mode=args.mode, endpoint=args.endpoint)
            print(f"stage=intake-delivery status=ok mode={args.mode} count={count}")
            return 0
        if args.command == "check-delivery":
            token = os.environ.pop("GATEX_INTELLIGENCE_INTAKE_SECRET", "")
            validate_delivery_configuration(endpoint=args.endpoint, token=token)
            print("stage=intake-delivery-config status=ok count=1")
            return 0
        if args.command == "inspect-config":
            intake = _intake_config(args.config)
            summary = {
                "enabled": True,
                "has_backfill_identity": bool(intake.get("tikhub_username")),
                "verification_status": str(intake.get("verification_status") or "unknown"),
            }
            print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
            return 0
        if args.command == "backfill-page":
            token = os.environ.pop("TIKHUB_WECHAT_TOKEN", "")
            count = run_backfill_page(
                config_path=args.config,
                state_path=args.state,
                state_out=args.state_out,
                output_path=args.output,
                token=token,
                maximum_items=args.maximum_items,
                base_url=args.base_url,
            )
            print(f"stage=backfill status=ok count={count}")
            return 0
        if args.command == "verify-profile":
            value = load_json(args.config)
            intake = value.get("intelligence_intake") if isinstance(value, dict) else None
            if not isinstance(intake, dict):
                raise RuntimeError("sealed profile has no intelligence intake configuration")
            username = str(intake.get("tikhub_username") or "").strip()
            publisher = str(intake.get("publisher") or "").strip()
            if not username or not publisher:
                raise RuntimeError("sealed candidate identity is unavailable")
            token = os.environ.pop("TIKHUB_WECHAT_TOKEN", "")
            verify_profile(
                TikHubTransport(token, base_url=args.base_url, max_calls=1),
                username,
                publisher,
            )
            print("stage=profile-verification status=ok count=1")
            return 0
    except BaseException as error:
        diagnostic = ""
        if isinstance(error, IntakeDeliveryError):
            diagnostic = " " + " ".join(error.diagnostic_fields())
        print(
            f"stage=intake status=failed error_type={type(error).__name__}{diagnostic}",
            file=sys.stderr,
        )
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
