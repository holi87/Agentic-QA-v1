"""Per-phase dispatch helpers (review-apply / run-tests / final-gate) (issue #292)."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

from .. import task_synthesis
from ..events import EventLog, event_log_for_paths
from ..paths import RuntimePaths
from ..runtime.tuning import (
    EVENTS_LOG_RING_SIZE as _EVENTS_LOG_RING_SIZE,
    RECORD_DETAIL_MAX_CHARS as _RECORD_DETAIL_MAX_CHARS,
    SHUTDOWN_GRACE_SECONDS as _SHUTDOWN_GRACE_SECONDS,
)
from ..storage import init_db



def _autonomy_review_then_apply(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    work_item_id: str,
) -> Dict[str, Any]:
    from ..orchestrator import Orchestrator
    from ..work_items import list_work_item_artifacts
    from ..workflows import run_review_gate

    orch = Orchestrator(conn, paths, events)
    patch_rel = None
    for art in list_work_item_artifacts(conn, work_item_id):
        if art["kind"] == "patch":
            patch_rel = art["path"]
    if patch_rel is None:
        return {"ok": False, "reason": "no_patch_artifact"}

    result = run_review_gate(
        orch,
        paths,
        events,
        diff_path=Path(patch_rel),
        scope="assertion",
        apply_patch_path=Path(patch_rel),
        work_item_id=work_item_id,
    )
    return {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "manifest_path": result.manifest_path,
    }

def _autonomy_run_tests(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    work_item_id: str,
) -> Dict[str, Any]:
    from ..orchestrator import Orchestrator
    from ..workflows import run_tests

    orch = Orchestrator(conn, paths, events)
    result = run_tests(orch, paths, events, work_item_id=work_item_id)
    return {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "failure_kind": result.failure_kind,
        "manifest_path": result.manifest_path,
    }

def _autonomy_final_gate(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    work_item_id: str,
) -> Dict[str, Any]:
    from ..orchestrator import Orchestrator
    from ..workflows import run_final_gate
    from ..workflows.stages.leases import GateBusy, serialized_gate

    # Issue #361 — the final gate runs exactly once, serially, per work item.
    # Once #360 fans out implementers, two agents could otherwise both reach the
    # final gate for the same work item and double-approve / double-merge. The
    # lease makes the loser refuse rather than queue behind the winner.
    try:
        with serialized_gate(conn, "final-gate", work_item_id):
            orch = Orchestrator(conn, paths, events)
            result = run_final_gate(orch, paths, events, work_item_id=work_item_id)
            return {
                "ok": result.ok,
                "exit_code": result.exit_code,
                "failure_kind": result.failure_kind,
                "manifest_path": result.manifest_path,
            }
    except GateBusy:
        events.write(
            "gate.final_lease_busy",
            severity="warning",
            payload={"work_item_id": work_item_id},
        )
        return {
            "ok": False,
            "exit_code": 2,
            "failure_kind": "infra",
            "manifest_path": None,
        }
