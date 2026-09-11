#!/usr/bin/env python3
"""Unified command-line interface for the VAS scanner workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core import (
    finalize_scan,
    next_candidates,
    prepare_scan,
    record_analysis,
    retry_candidate,
)
from config import DEFAULT_MAX_CANDIDATES


def positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def candidate_limit(value: str) -> int | None:
    if value.lower() == "all":
        return None
    return positive_integer(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the agent-driven vas-scanner scan.version 1 workflow."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="Discover, rank, and render candidate hotspots.")
    prepare.add_argument("vas_id", help="Bundled VAS rule id, for example VAS-0003")
    prepare.add_argument("repo_path", type=Path, help="Target repository directory")
    prepare.add_argument(
        "--max-candidates",
        type=candidate_limit,
        default=DEFAULT_MAX_CANDIDATES,
        metavar="N|all",
        help=(
            "Schedule the top N admitted files, or all admitted files "
            f"(default: {DEFAULT_MAX_CANDIDATES})"
        ),
    )

    next_command = commands.add_parser("next", help="Claim the next candidate batch.")
    next_command.add_argument("scan_dir", type=Path, help="Scan directory returned by prepare")
    next_command.add_argument(
        "--limit",
        type=positive_integer,
        help="Maximum number of newly claimed tasks (default: scan setting, normally 3)",
    )

    record = commands.add_parser("record", help="Record one candidate's warning array from stdin.")
    record.add_argument("scan_dir", type=Path, help="Scan directory returned by prepare")
    record.add_argument("rank", type=int, help="Candidate rank")

    retry = commands.add_parser("retry", help="Record an analysis error from stdin and retry a candidate.")
    retry.add_argument("scan_dir", type=Path, help="Scan directory returned by prepare")
    retry.add_argument("rank", type=int, help="Candidate rank")

    finalize = commands.add_parser("finalize", help="Deduplicate and finalize report.json.")
    finalize.add_argument("scan_dir", type=Path, help="Scan directory returned by prepare")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    exit_code = 0
    try:
        if args.command == "prepare":
            scan_dir = prepare_scan(
                args.vas_id,
                args.repo_path,
                max_candidates=args.max_candidates,
            )
            report = json.loads((scan_dir / "report.json").read_text(encoding="utf-8"))
            result = {
                "scan_dir": str(scan_dir),
                "coverage": report["coverage"],
            }
        elif args.command == "next":
            result = next_candidates(args.scan_dir, limit=args.limit)
        elif args.command == "record":
            result = record_analysis(args.scan_dir, args.rank, json.load(sys.stdin))
        elif args.command == "retry":
            result = retry_candidate(args.scan_dir, args.rank, sys.stdin.read())
        else:
            result = finalize_scan(args.scan_dir)
            if result["status"] == "incomplete":
                exit_code = 2
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
