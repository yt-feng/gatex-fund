from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .engine import private_failure, run_pipeline
from .guard import guard_public_tree
from .packing import deterministic_tar


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="snapshot-pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--provider", type=Path, required=True)
    run.add_argument("--state", type=Path, required=True)
    run.add_argument("--state-out", type=Path, required=True)
    run.add_argument("--work", type=Path, required=True)
    run.add_argument("--result", type=Path, required=True)

    pack = subparsers.add_parser("pack")
    pack.add_argument("--source", type=Path, required=True)
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--id-file", type=Path, required=True)

    guard = subparsers.add_parser("guard")
    guard.add_argument("--root", type=Path, required=True)
    guard.add_argument("--config", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            run_pipeline(
                config_path=args.config,
                provider_path=args.provider,
                state_path=args.state,
                state_out=args.state_out,
                work_dir=args.work,
                result_path=args.result,
            )
            return 0
        if args.command == "pack":
            identifier = deterministic_tar(args.source, args.output)
            args.id_file.write_text(identifier + "\n", encoding="ascii")
            print("stage=run status=ok count=1")
            return 0
        if args.command == "guard":
            return guard_public_tree(args.root, args.config)
    except BaseException as error:
        work = getattr(args, "work", Path.cwd())
        error_id = private_failure(work, error)
        print(f"stage=run status=failed error_id={error_id}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
