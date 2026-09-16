#!/usr/bin/env python3
"""CLI for the standalone VAS scanner workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core import finalize_scan, preflight_scan, prepare_scan, record_analysis


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the standalone VAS scanner workflow.")
    commands = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("preflight", "Check scanner dependencies and the target repository."),
        ("prepare", "Discover anchors and create an immutable task manifest."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("vas_id", help="Bundled VAS rule id, for example VAS-0003")
        command.add_argument("repo_path", type=Path, help="Target repository directory")

    record = commands.add_parser("record", help="Validate and write one task result.")
    record.add_argument("run_dir", type=Path, help="Run directory returned by prepare")
    record.add_argument("task_id", help="Task identifier, for example TASK-0001")

    finalize = commands.add_parser("finalize", help="Validate task results and write report.json.")
    finalize.add_argument("run_dir", type=Path, help="Run directory returned by prepare")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "preflight":
            result = preflight_scan(args.vas_id, args.repo_path)
        elif args.command == "prepare":
            result = prepare_scan(args.vas_id, args.repo_path)
        elif args.command == "record":
            result = record_analysis(args.run_dir, args.task_id, json.load(sys.stdin))
        else:
            result = finalize_scan(args.run_dir)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
