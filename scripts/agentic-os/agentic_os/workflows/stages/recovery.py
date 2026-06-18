"""Recovery + abandon-patch helpers (issue #292)."""
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
from .dry_run import env_hash, write_manifest



def run_recovery(
    orchestrator: Orchestrator,
    paths: RuntimePaths,
    events: EventLog,
) -> WorkflowResult:
    orchestrator.seed_phases()
    task_id = orchestrator.create_task(
        phase_id=CURRENT_PHASE_ID,
        kind="recovery",
        payload={"workflow": "recovery"},
    )
    orchestrator.lease_task(task_id, owner="orchestrator", ttl_seconds=60)
    orchestrator.mark_running(task_id, owner="orchestrator")
    started_at = now_iso()
    run_id_str = new_run_id()
    log_path = paths.subprocess_logs_dir / f"{run_id_str}.log"
    log_path.write_text("recovery scan\n", encoding="utf-8")
    orchestrator.record_run(
        task_id=task_id,
        run_id=run_id_str,
        command=["python", "-m", "agentic_os", "run", "recovery"],
        cwd=str(paths.repo_root),
        env_hash=env_hash(),
        log_path=str(log_path.relative_to(paths.repo_root)),
        started_at=started_at,
    )
    scan = orchestrator.recovery_scan()
    finished_at = now_iso()
    manifest_path = write_manifest(
        paths=paths,
        run_id_str=run_id_str,
        task_id=task_id,
        kind="recovery",
        command=["python", "-m", "agentic_os", "run", "recovery"],
        cwd=str(paths.repo_root),
        started_at=started_at,
        finished_at=finished_at,
        exit_code=0,
        failure_kind=None,
        extra={"recovery": scan},
    )
    orchestrator.finish_run(
        run_id=run_id_str,
        exit_code=0,
        duration_ms=0,
        failure_kind=None,
        unmapped_exit=False,
        evidence_path=str((paths.evidence_dir / run_id_str).relative_to(paths.repo_root)),
        manifest_path=str(manifest_path.relative_to(paths.repo_root)),
        finished_at=finished_at,
    )
    orchestrator.finish_task(task_id, status="succeeded", exit_code=0)
    events.write(
        "recovery.completed",
        payload={
            "task_id": task_id,
            "run_id": run_id_str,
            "scan": scan,
        },
    )
    return WorkflowResult(
        ok=True,
        exit_code=0,
        failure_kind=None,
        task_id=task_id,
        run_id=run_id_str,
        manifest_path=str(manifest_path.relative_to(paths.repo_root)),
        reports_path=None,
        bugs_opened=[],
    )

def abandon_patch(
    paths: RuntimePaths,
    events: EventLog,
    *,
    task_id: str,
    patch_path: str,
    reason: str,
    operator: str = "operator",
) -> Dict[str, Any]:
    """Abandon a patch artifact for a work item.

    Records a gate artifact with `verdict: ABANDONED`, a decision row, and
    updates the work item so the dashboard can stop showing the patch as a
    blocker. History is preserved — the patch row is not deleted.
    """
    from ...storage.db import transaction
    from ...work_items import register_work_item_artifact

    reason = (reason or "").strip()
    if not reason:
        raise UsageError("abandon-patch requires a non-empty --reason")

    resolved = resolve_repo_path(
        paths.repo_root, patch_path, label="patch path", must_exist=False
    )
    rel_patch = str(resolved.resolve().relative_to(paths.repo_root.resolve()))

    conn = _db_connect(paths.db)
    try:
        patch_row = conn.execute(
            """
            SELECT id, work_item_id
              FROM work_item_artifacts
             WHERE work_item_id=? AND kind='patch' AND path=?
             LIMIT 1;
            """,
            (task_id, rel_patch),
        ).fetchone()
        if patch_row is None:
            raise UsageError(
                f"no patch artifact found for task {task_id} at {rel_patch}"
            )

        artifact_path = write_abandon_artifact(
            paths,
            patch_rel_path=rel_patch,
            reason=reason,
            operator=operator,
        )
        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=task_id,
            kind="gate",
            path=str(artifact_path.relative_to(paths.repo_root)),
        )

        decision_id = ulid()
        decided_at = now_iso()
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO decisions(
                    id, phase_id, topic, decided_by, rationale, consequences, decided_at
                ) VALUES (?, ?, ?, 'operator', ?, ?, ?);
                """,
                (
                    decision_id,
                    CURRENT_PHASE_ID,
                    f"patch_abandoned:{rel_patch}",
                    reason,
                    f"patch {rel_patch} abandoned for task {task_id}; final gate skips it",
                    decided_at,
                ),
            )
    finally:
        conn.close()

    events.write(
        "gate.patch_abandoned",
        actor=operator,
        payload={
            "task_id": task_id,
            "patch_path": rel_patch,
            "reason": reason,
            "decision_id": decision_id,
            "artifact_path": str(artifact_path.relative_to(paths.repo_root)),
        },
    )
    return {
        "task_id": task_id,
        "patch_path": rel_patch,
        "reason": reason,
        "decision_id": decision_id,
        "artifact_path": str(artifact_path.relative_to(paths.repo_root)),
    }
