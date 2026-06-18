"""Reviewer-lease helpers (issue #292)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

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



def _lease_expiry(ttl_seconds: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---- gate serialization (issue #361) -------------------------------------
#
# Gates run **exactly once, serially**, on the merged artifact — never fanned
# out (AGENTS.md § "Parallel agent orchestration"). The review gate is already
# serialized by the per-work-item ``work_items.reviewer_lease`` token. The
# FINAL gate (evaluate_final_gate + merge_patch_if_approved) had no guard, so
# two parallel agents could both approve and merge the same patch — a
# double-approval. ``serialized_gate`` closes that hole.
#
# Why the shared ``leases`` table and not new ``work_items`` columns: adding
# columns needs a migration + a SCHEMA_VERSION bump, which would activate the
# currently-dormant migration 17 (``ALTER TABLE model_invocations ADD COLUMN
# work_item_id`` — already present in schema.sql) and crash live DBs with
# "duplicate column". The ``leases`` table already exists, its CAS is
# serialized by ``BEGIN IMMEDIATE``, and ``doctor --repair`` already reclaims a
# lease whose owning process died (real pid/host) — so a crashed gate holder is
# auto-recovered. The token-equivalent fence is ``acquired_at`` (ms precision,
# unique per acquisition): release deletes only the exact row it acquired, so a
# stale handle from a superseded holder cannot clear a live lease.


class GateBusy(RuntimeError):
    """Raised when a gate is already being run for this work item."""


@dataclass(frozen=True)
class GateLease:
    gate: str
    work_item_id: str
    owner: str
    acquired_at: str
    expires_at: str


def _gate_owner(gate: str, work_item_id: str) -> str:
    return f"{gate}:{work_item_id}"


def acquire_gate_lease(
    conn: sqlite3.Connection,
    gate: str,
    work_item_id: str,
    *,
    ttl_seconds: int = 900,
) -> Optional[GateLease]:
    """Compare-and-swap a serialization lease for ``gate`` on ``work_item_id``.

    Returns a :class:`GateLease` handle on success, or ``None`` when an
    unexpired lease is already held (the caller must NOT run the gate).
    """
    owner = _gate_owner(gate, work_item_id)
    now = now_iso()
    expires = _lease_expiry(ttl_seconds)
    acquired = False
    with transaction(conn):
        row = conn.execute(
            "SELECT expires_at FROM leases WHERE owner=?;", (owner,)
        ).fetchone()
        held = row is not None and isinstance(row["expires_at"], str) and row["expires_at"] > now
        if not held:
            conn.execute(
                """
                INSERT INTO leases(owner, pid, host, acquired_at, expires_at, heartbeat_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner) DO UPDATE SET
                    pid=excluded.pid, host=excluded.host,
                    acquired_at=excluded.acquired_at,
                    expires_at=excluded.expires_at,
                    heartbeat_at=excluded.heartbeat_at;
                """,
                (owner, os.getpid(), socket.gethostname(), now, expires, now),
            )
            acquired = True
    if not acquired:
        return None
    return GateLease(
        gate=gate,
        work_item_id=work_item_id,
        owner=owner,
        acquired_at=now,
        expires_at=expires,
    )


def release_gate_lease(conn: sqlite3.Connection, lease: GateLease) -> bool:
    """Release a gate lease. Fenced by ``acquired_at`` so a stale handle (the
    holder was already superseded after TTL expiry) cannot clear a live lease.
    Returns True iff this call actually cleared the row."""
    with transaction(conn):
        cur = conn.execute(
            "DELETE FROM leases WHERE owner=? AND acquired_at=?;",
            (lease.owner, lease.acquired_at),
        )
        released = cur.rowcount > 0
    return released


@contextmanager
def serialized_gate(
    conn: sqlite3.Connection,
    gate: str,
    work_item_id: str,
    *,
    ttl_seconds: int = 900,
) -> Iterator[GateLease]:
    """Run a gate body exactly once. Raises :class:`GateBusy` if another agent
    already holds the gate for this work item (no double-approval)."""
    lease = acquire_gate_lease(conn, gate, work_item_id, ttl_seconds=ttl_seconds)
    if lease is None:
        raise GateBusy(f"{gate} already in progress for work item {work_item_id}")
    try:
        yield lease
    finally:
        release_gate_lease(conn, lease)

def _acquire_reviewer_lease(
    conn: sqlite3.Connection,
    work_item_id: str,
    *,
    token: str,
    ttl_seconds: int = 900,
) -> bool:
    now = now_iso()
    expires = _lease_expiry(ttl_seconds)
    with transaction(conn):
        row = conn.execute(
            """
            SELECT reviewer_lease, reviewer_lease_expires
              FROM work_items
             WHERE id=?;
            """,
            (work_item_id,),
        ).fetchone()
        if row is None:
            return False
        active_lease = row["reviewer_lease"]
        active_until = row["reviewer_lease_expires"]
        if active_lease and isinstance(active_until, str) and active_until > now:
            return False
        conn.execute(
            """
            UPDATE work_items
               SET reviewer_lease=?, reviewer_lease_expires=?, updated_at=?
             WHERE id=?;
            """,
            (token, expires, now, work_item_id),
        )
    return True

def _release_reviewer_lease(
    conn: sqlite3.Connection,
    work_item_id: str,
    *,
    token: str,
) -> None:
    now = now_iso()
    with transaction(conn):
        conn.execute(
            """
            UPDATE work_items
               SET reviewer_lease=NULL, reviewer_lease_expires=NULL, updated_at=?
             WHERE id=? AND reviewer_lease=?;
            """,
            (now, work_item_id, token),
        )

def _review_gate_busy_result(
    orchestrator: Orchestrator,
    paths: RuntimePaths,
    events: EventLog,
    *,
    task_id: str,
    scope: str,
    work_item_id: str,
) -> WorkflowResult:
    started_at = now_iso()
    finished_at = started_at
    run_id_str = new_run_id()
    log_path = paths.subprocess_logs_dir / f"{run_id_str}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("review gate skipped: reviewer lease is already active\n", encoding="utf-8")
    command = [
        "agentic-os",
        "run",
        "review-gate",
        "--scope",
        scope,
        "--work-item",
        work_item_id,
    ]
    orchestrator.record_run(
        task_id=task_id,
        run_id=run_id_str,
        command=command,
        cwd=str(paths.repo_root),
        env_hash=env_hash(),
        log_path=str(log_path.relative_to(paths.repo_root)),
        started_at=started_at,
    )
    manifest_path = write_manifest(
        paths=paths,
        run_id_str=run_id_str,
        task_id=task_id,
        kind="review-gate",
        command=command,
        cwd=str(paths.repo_root),
        started_at=started_at,
        finished_at=finished_at,
        exit_code=2,
        failure_kind="infra",
        extra={"error": "reviewer_lease_busy", "work_item_id": work_item_id},
    )
    orchestrator.finish_run(
        run_id=run_id_str,
        exit_code=2,
        duration_ms=0,
        failure_kind="infra",
        unmapped_exit=False,
        evidence_path=str((paths.evidence_dir / run_id_str).relative_to(paths.repo_root)),
        manifest_path=str(manifest_path.relative_to(paths.repo_root)),
        finished_at=finished_at,
    )
    orchestrator.finish_task(
        task_id,
        status="failed",
        exit_code=2,
        error_class="reviewer_lease_busy",
    )
    events.write(
        "gate.review_lease_busy",
        task_id=task_id,
        run_id=run_id_str,
        severity="warning",
        payload={"work_item_id": work_item_id},
    )
    return WorkflowResult(
        ok=False,
        exit_code=2,
        failure_kind="infra",
        task_id=task_id,
        run_id=run_id_str,
        manifest_path=str(manifest_path.relative_to(paths.repo_root)),
        reports_path=None,
        bugs_opened=[],
    )
