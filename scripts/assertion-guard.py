#!/usr/bin/env python3
"""Block silent assertion weakening unless a decision id is supplied."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "agentic-os"))

from agentic_os.assertions import guard_files  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="assertion-guard")
    parser.add_argument("--before", required=True, type=Path, help="file before the proposed change")
    parser.add_argument("--after", required=True, type=Path, help="file after the proposed change")
    parser.add_argument("--file-path", default=None, help="logical path stored in assertion_changes")
    parser.add_argument("--decision-id", default=None, help="existing decision that allows weakening")
    parser.add_argument("--db", type=Path, default=None, help="optional Agentic OS SQLite DB to record changes")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = guard_files(
        before_path=args.before,
        after_path=args.after,
        file_path=args.file_path,
        decision_id=args.decision_id,
        db_path=args.db,
        task_id=args.task_id,
        run_id=args.run_id,
    )

    if args.json:
        sys.stdout.write(json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n")
    elif result.ok:
        sys.stdout.write("assertion guard: ok\n")
    else:
        sys.stderr.write(
            "assertion guard: blocked\n"
            f"  weakened: {result.blocked}\n"
            f"  needs_decision: {result.needs_decision}\n"
        )
        for change in result.changes:
            if change.classification in {"weakened", "unknown"}:
                sys.stderr.write(
                    f"  {change.file_path}: {change.classification} ({change.reason})\n"
                )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
