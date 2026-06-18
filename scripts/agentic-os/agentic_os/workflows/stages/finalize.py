"""Report finalization (issue #292)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...atomic_io import atomic_write_json
from ...errors import UsageError
from ...events import EventLog
from ...gates import (
    GateFinding,
    GateResult,
    evaluate_final_gate,
    final_gate,
    merge_patch_if_approved,
    parse_gate_output,
    static_review_gate,
    write_abandon_artifact,
    write_gate_result,
)
from ...ids import ulid
from ...ids import run_id as new_run_id
from ...orchestrator import CURRENT_PHASE_ID, Orchestrator
from ...paths import RuntimePaths
from ...runtime.subprocess import run_command, scrub_provider_credentials
from ...security import resolve_repo_path
from ...storage.db import connect as _db_connect, transaction
from ...time_utils import now_iso



def _zero_test_report_status(paths: RuntimePaths) -> tuple[bool, bool]:
    """Return `(zero_tests, discovery_only)` for `reports/last-run.json`.

    Issue #100 — a runner that exits 0 with `total=0` is usually a
    silent infra failure (missing JUnit, broken discovery). The
    explicit escape hatch is `discovery_only: true` (alias `dry_run`)
    in the report payload.
    """
    last_run = paths.repo_root / "reports" / "last-run.json"
    if not last_run.is_file():
        return False, False
    try:
        data = json.loads(last_run.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, False
    if not isinstance(data, dict):
        return False, False
    total = data.get("total")
    if not isinstance(total, int) or total != 0:
        return False, False
    # Strict boolean check — codex review on #129. A truthy string
    # like `"false"` or a number must NOT silently bypass the block.
    discovery_only = (
        data.get("discovery_only") is True or data.get("dry_run") is True
    )
    return True, discovery_only

def finalize_reports(paths: RuntimePaths, events: EventLog) -> tuple[bool, List[str]]:
    from ... import qualitycat

    errors: List[str] = []
    try:
        copy = qualitycat.copy_reports(paths, events, clean=True)
        if copy.exit_code != 0:
            errors.append(f"copy-reports exit={copy.exit_code}")
    except Exception as exc:  # report finalization must not hide product failures
        errors.append(f"copy-reports error: {exc}")
    try:
        qualitycat.extract_last_run(paths, events)
    except Exception as exc:
        errors.append(f"extract-last-run error: {exc}")
    try:
        summary = qualitycat.build_summary(paths, events)
        if summary.exit_code != 0:
            errors.append(f"build-summary exit={summary.exit_code}")
    except Exception as exc:
        errors.append(f"build-summary error: {exc}")

    for rel in ("reports/last-run.json", "reports/summary.md"):
        if not (paths.repo_root / rel).exists():
            errors.append(f"missing {rel}")

    return not errors, errors
