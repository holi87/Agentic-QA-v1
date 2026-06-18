"""Idempotency-key helpers for run-tests replay (issue #292)."""
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
from ._types import WorkflowResult



def _hash_file_if_available(paths: RuntimePaths, rel_path: str) -> Optional[str]:
    target = (paths.repo_root / rel_path).resolve()
    try:
        target.relative_to(paths.repo_root.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return None

def _work_item_test_inputs_fingerprint(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    work_item_id: str,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT kind, path, created_at
          FROM work_item_artifacts
         WHERE work_item_id=?
           AND kind IN ('spec','analysis','test_plan','patch','gate','apply')
         ORDER BY created_at DESC, id DESC
         LIMIT 40;
        """,
        (work_item_id,),
    ).fetchall()
    return [
        {
            "kind": str(row["kind"]),
            "path": str(row["path"]),
            "sha256": _hash_file_if_available(paths, str(row["path"])),
        }
        for row in rows
    ]

def _run_tests_idempotency_key(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    *,
    work_item_id: Optional[str],
    tag: Optional[str],
    command: List[str],
) -> Optional[str]:
    if work_item_id is None:
        return None
    payload = {
        "workflow": "run-tests",
        "work_item_id": work_item_id,
        "tag": tag,
        "command": command,
        "inputs": _work_item_test_inputs_fingerprint(conn, paths, work_item_id),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"run-tests:{digest}"

def _find_run_by_idempotency_key(
    conn: sqlite3.Connection,
    idempotency_key: Optional[str],
) -> Optional[sqlite3.Row]:
    if idempotency_key is None:
        return None
    return conn.execute(
        """
        SELECT id, task_id, exit_code, failure_kind, manifest_path, finished_at
          FROM runs
         WHERE idempotency_key=?
         ORDER BY started_at DESC
         LIMIT 1;
        """,
        (idempotency_key,),
    ).fetchone()

def _workflow_result_from_run_row(paths: RuntimePaths, row: sqlite3.Row) -> WorkflowResult:
    exit_code = int(row["exit_code"]) if row["exit_code"] is not None else 2
    failure_kind = row["failure_kind"] if row["failure_kind"] is not None else (
        "infra" if row["finished_at"] is None else None
    )
    manifest_rel = str(row["manifest_path"] or "")
    reports_path: Optional[str] = None
    bugs_opened: List[str] = []
    if manifest_rel:
        manifest = paths.repo_root / manifest_rel
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        effective_exit = data.get("effective_exit_code")
        if isinstance(effective_exit, int):
            exit_code = effective_exit
        effective_failure = data.get("effective_failure_kind")
        if effective_failure is None or isinstance(effective_failure, str):
            failure_kind = effective_failure
        reports = data.get("reports") if isinstance(data.get("reports"), dict) else {}
        if reports.get("finalized"):
            reports_path = str(reports.get("path") or "reports")
        triage = data.get("triage") if isinstance(data.get("triage"), dict) else {}
        bugs_opened = list(triage.get("bugs_opened") or [])
    return WorkflowResult(
        ok=exit_code == 0,
        exit_code=exit_code,
        failure_kind=failure_kind,
        task_id=str(row["task_id"]),
        run_id=str(row["id"]),
        manifest_path=manifest_rel,
        reports_path=reports_path,
        bugs_opened=bugs_opened,
    )
