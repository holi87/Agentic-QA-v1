"""Final-gate pipeline stage (issue #292)."""
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
from .attachments import _attach_run_artifacts_to_work_item
from .dry_run import _summarize_model_roles_for_manifest, env_hash, write_manifest



def run_final_gate(
    orchestrator: Orchestrator,
    paths: RuntimePaths,
    events: EventLog,
    *,
    work_item_id: Optional[str] = None,
) -> WorkflowResult:
    orchestrator.seed_phases()
    task_id = orchestrator.create_task(
        phase_id=CURRENT_PHASE_ID,
        kind="review",
        payload={"workflow": "final-gate"},
    )
    orchestrator.lease_task(task_id, owner="orchestrator", ttl_seconds=60)
    orchestrator.mark_running(task_id, owner="orchestrator")

    started_at = now_iso()
    run_id_str = new_run_id()
    # Issue #184 — when --work-item is supplied, the gate needs DB
    # access to verify a real run-tests run attested for that work item.
    conn_for_gate = None
    if work_item_id is not None:
        try:
            conn_for_gate = _db_connect(paths.db)
        except Exception:
            conn_for_gate = None
    try:
        gate, pillars = evaluate_final_gate(
            paths, conn=conn_for_gate, work_item_id=work_item_id
        )
    finally:
        if conn_for_gate is not None:
            conn_for_gate.close()
    gate_path = write_gate_result(paths, gate, name="final-gate")
    log_path = paths.subprocess_logs_dir / f"{run_id_str}.log"
    log_path.write_text(gate.to_text(), encoding="utf-8")

    # Issue #111 — keep manifest self-sufficient for audit.
    full_command: List[str] = ["agentic-os", "run", "final-gate"]
    if work_item_id is not None:
        full_command.extend(["--work-item", work_item_id])
    audit_context: Dict[str, Any] = {
        "work_item_id": work_item_id,
        "gate_output_path": str(gate_path.relative_to(paths.repo_root)),
        "pillars_evaluated": list(pillars.keys()),
    }

    orchestrator.record_run(
        task_id=task_id,
        run_id=run_id_str,
        command=full_command,
        cwd=str(paths.repo_root),
        env_hash=env_hash(),
        log_path=str(log_path.relative_to(paths.repo_root)),
        started_at=started_at,
    )
    finished_at = now_iso()
    exit_code = 0 if gate.approved else 1
    manifest_path = write_manifest(
        paths=paths,
        run_id_str=run_id_str,
        task_id=task_id,
        kind="final-gate",
        command=full_command,
        cwd=str(paths.repo_root),
        started_at=started_at,
        finished_at=finished_at,
        exit_code=exit_code,
        failure_kind=None if gate.approved else "product",
        extra={
            "gate": gate.to_json(),
            "gate_output_path": str(gate_path.relative_to(paths.repo_root)),
            "pillars": pillars,
            "audit_context": audit_context,
            # Issue #101 — surface model role wiring so dashboard/CLI
            # can show which roles were configured vs. invoked. Real
            # invocation is gated on `models.<role>.auto_fire=true`
            # and remains opt-in.
            "models_roles": _summarize_model_roles_for_manifest(paths),
        },
    )
    orchestrator.finish_run(
        run_id=run_id_str,
        exit_code=exit_code,
        duration_ms=0,
        failure_kind=None if gate.approved else "product",
        unmapped_exit=False,
        evidence_path=str((paths.evidence_dir / run_id_str).relative_to(paths.repo_root)),
        manifest_path=str(manifest_path.relative_to(paths.repo_root)),
        finished_at=finished_at,
    )
    orchestrator.finish_task(
        task_id,
        status="succeeded" if gate.approved else "failed",
        exit_code=exit_code,
        error_class=None if gate.approved else gate.reason,
    )
    events.write(
        "gate.final_finished",
        task_id=task_id,
        run_id=run_id_str,
        severity="info" if gate.approved else "warning",
        payload=gate.to_json(),
    )
    result_obj = WorkflowResult(
        ok=gate.approved,
        exit_code=exit_code,
        failure_kind=None if gate.approved else "product",
        task_id=task_id,
        run_id=run_id_str,
        manifest_path=str(manifest_path.relative_to(paths.repo_root)),
        reports_path=None,
        bugs_opened=[],
    )
    if work_item_id is not None:
        _attach_run_artifacts_to_work_item(
            paths=paths,
            events=events,
            work_item_id=work_item_id,
            kind="final-gate",
            result=result_obj,
            evidence_subdir=run_id_str,
            report_ok=False,
            extra_gate_path=gate_path,
        )
    return result_obj
