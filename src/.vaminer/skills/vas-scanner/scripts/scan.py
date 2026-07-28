#!/usr/bin/env python3
"""Unified command-line interface for the VAS scanner workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core import finalize_scan, next_candidates, prepare_scan, record_analysis, retry_candidate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a platform-independent VAS scan workflow.")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="Discover, rank, and render candidate hotspots.")
    prepare.add_argument("vas_id", help="Bundled VAS rule id, for example VAS-0003")
    prepare.add_argument("repo_path", type=Path, help="Target repository directory")

    next_command = commands.add_parser("next", help="Claim the next candidate batch.")
    next_command.add_argument("scan_dir", type=Path, help="Scan directory returned by prepare")

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
    try:
        if args.command == "prepare":
            scan_dir = prepare_scan(args.vas_id, args.repo_path)
            scan = json.loads((scan_dir / "scan.json").read_text(encoding="utf-8"))
            result = {
                "scan_dir": str(scan_dir),
                "candidate_count": len(scan["candidates"]),
            }
        elif args.command == "next":
            result = next_candidates(args.scan_dir)
        elif args.command == "record":
            result = record_analysis(args.scan_dir, args.rank, json.load(sys.stdin))
        elif args.command == "retry":
            result = retry_candidate(args.scan_dir, args.rank, sys.stdin.read())
        else:
            result = finalize_scan(args.scan_dir)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

