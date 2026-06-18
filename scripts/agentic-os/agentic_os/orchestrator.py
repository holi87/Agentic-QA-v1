"""Phase machine, task lifecycle, lease and recovery scan."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .atomic_io import atomic_write_json
from .events import EventLog, event_log_for_paths
from .ids import ulid
from .paths import RuntimePaths
from .storage import init_db
from .storage.db import integrity_check, transaction
from .time_utils import now_iso


SEEDED_PHASES = (
    ("02-codex-runtime-contract", "phase/02-codex-runtime-contract"),
    ("03-sonnet-core-runtime", "phase/03-sonnet-core-runtime"),
    ("04-codex-persistence-guards", "phase/04-codex-persistence-guards"),
    ("05-sonnet-dashboard-runner", "phase/05-sonnet-dashboard-runner"),
    ("06-opus-bug-adjudication", "phase/06-opus-bug-adjudication"),
    ("07-sonnet-qualitycat-integration", "phase/07-sonnet-qualitycat-integration"),
    ("08-codex-final-gates-hardening", "phase/08-codex-final-gates-hardening"),
    ("09-sonnet-fake-sut-dry-run", "phase/09-sonnet-fake-sut-dry-run"),
    ("10-codex-dashboard-task-intake", "phase/10-codex-dashboard-task-intake"),
    ("11-sonnet-dashboard-task-ui", "phase/11-sonnet-dashboard-task-ui"),
    ("12-sonnet-sut-analysis-test-planning", "phase/12-sonnet-sut-analysis-test-planning"),
    ("13-sonnet-codex-patch-generation-gated-merge", "phase/13-sonnet-codex-patch-generation-gated-merge"),
    ("14-sonnet-dashboard-run-e2e", "phase/14-sonnet-dashboard-run-e2e"),
    ("15-sonnet-final-fake-sut-dry-run", "phase/15-sonnet-final-fake-sut-dry-run"),
    ("16-sonnet-dashboard-visual-polish", "phase/16-sonnet-dashboard-visual-polish"),
)
CURRENT_PHASE_ID = "14-sonnet-dashboard-run-e2e"


@dataclass
class TaskRow:
    id: str
    phase_id: str
    kind: str
    status: str
    payload: Dict[str, Any]
    lease_owner: Optional[str]
    lease_expires: Optional[str]


class Orchestrator:
    def __init__(self, conn: sqlite3.Connection, paths: RuntimePaths, events: EventLog):
        self.conn = conn
        self.paths = paths
        self.events = events

    # ---- bootstrap --------------------------------------------------------

    def seed_phases(self) -> int:
        inserted = 0
        ts = now_iso()
        with transaction(self.conn):
            for phase_id, branch in SEEDED_PHASES:
                spec_path = f"docs/phases/{phase_id}.md"
                cur = self.conn.execute("SELECT id FROM phases WHERE id=?;", (phase_id,))
                if cur.fetchone() is None:
                    self.conn.execute(
                        """
                        INSERT INTO phases(id, status, branch, spec_path, updated_at)
                        VALUES (?, 'planned', ?, ?, ?);
                        """,
                        (phase_id, branch, spec_path, ts),
                    )
                    inserted += 1
        if inserted:
            self.events.write(
                "phase.seeded",
                payload={"count": inserted, "current": CURRENT_PHASE_ID},
            )
        return inserted

    # ---- tasks ------------------------------------------------------------

    def create_task(
        self,
        *,
        phase_id: str,
        kind: str,
        payload: Dict[str, Any],
        parent_id: Optional[str] = None,
    ) -> str:
        task_id = ulid()
        ts = now_iso()
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO tasks(id, phase_id, parent_id, kind, status, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?, ?);
                """,
                (task_id, phase_id, parent_id, kind, json.dumps(payload, sort_keys=True), ts, ts),
            )
        self.events.write(
            "task.created",
            phase_id=phase_id,
            task_id=task_id,
            payload={"kind": kind, "workflow": payload.get("workflow")},
        )
        return task_id

    def lease_task(self, task_id: str, *, owner: str, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        ts = now_iso()
        expires = _iso_plus_seconds(ts, ttl_seconds)
        with transaction(self.conn):
            cur = self.conn.execute(
                "SELECT status, lease_owner, lease_expires FROM tasks WHERE id=?;",
                (task_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown task: {task_id}")
            if row["status"] not in ("queued", "leased"):
                raise RuntimeError(f"task {task_id} cannot be leased from status {row['status']}")
            if (
                row["status"] == "leased"
                and row["lease_expires"] is not None
                and row["lease_expires"] > ts
                and row["lease_owner"] != owner
            ):
                raise RuntimeError(
                    f"task {task_id} is leased by {row['lease_owner']} until {row['lease_expires']}"
                )
            self.conn.execute(
                """
                UPDATE tasks
                   SET status='leased', lease_owner=?, lease_expires=?, updated_at=?
                 WHERE id=?;
                """,
                (owner, expires, ts, task_id),
            )
        self.events.write(
            "task.leased",
            task_id=task_id,
            payload={"owner": owner, "expires_at": expires},
        )

    def renew_task_lease(self, task_id: str, *, owner: str, ttl_seconds: int) -> str:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        ts = now_iso()
        expires = _iso_plus_seconds(ts, ttl_seconds)
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT status, lease_owner FROM tasks WHERE id=?;",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task: {task_id}")
            if row["status"] not in ("leased", "running"):
                raise RuntimeError(f"task {task_id} cannot renew lease from status {row['status']}")
            if row["lease_owner"] != owner:
                raise RuntimeError(f"task {task_id} is leased by {row['lease_owner']}, not {owner}")
            self.conn.execute(
                """
                UPDATE tasks
                   SET lease_expires=?, updated_at=?
                 WHERE id=?;
                """,
                (expires, ts, task_id),
            )
        self.events.write(
            "task.lease_renewed",
            task_id=task_id,
            payload={"owner": owner, "expires_at": expires},
        )
        return expires

    def mark_running(self, task_id: str, *, owner: Optional[str] = None) -> None:
        ts = now_iso()
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT status, lease_owner FROM tasks WHERE id=?;",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task: {task_id}")
            if row["status"] not in ("leased", "running"):
                raise RuntimeError(f"task {task_id} cannot start from status {row['status']}")
            if owner is not None and row["lease_owner"] != owner:
                raise RuntimeError(f"task {task_id} is leased by {row['lease_owner']}, not {owner}")
            self.conn.execute(
                """
                UPDATE tasks SET status='running', started_at=COALESCE(started_at, ?), updated_at=?
                 WHERE id=?;
                """,
                (ts, ts, task_id),
            )
        self.events.write("task.started", task_id=task_id, payload={})

    def finish_task(
        self,
        task_id: str,
        *,
        status: str,
        exit_code: Optional[int] = None,
        error_class: Optional[str] = None,
    ) -> None:
        if status not in ("succeeded", "failed", "cancelled", "timeout"):
            raise ValueError(f"invalid terminal status: {status}")
        ts = now_iso()
        with transaction(self.conn):
            self.conn.execute(
                """
                UPDATE tasks
                   SET status=?, finished_at=?, updated_at=?, exit_code=?, error_class=?,
                       lease_owner=NULL, lease_expires=NULL
                 WHERE id=?;
                """,
                (status, ts, ts, exit_code, error_class, task_id),
            )
        self.events.write(
            "task.finished",
            task_id=task_id,
            severity="info" if status == "succeeded" else "warning",
            payload={"status": status, "exit_code": exit_code, "error_class": error_class},
        )

    # ---- runs -------------------------------------------------------------

    def record_run(
        self,
        *,
        task_id: str,
        run_id: str,
        idempotency_key: Optional[str] = None,
        command: List[str],
        cwd: str,
        env_hash: str,
        log_path: str,
        started_at: str,
    ) -> None:
        with transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO runs(
                  id, task_id, idempotency_key, command, cwd, env_hash, log_path, started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    run_id,
                    task_id,
                    idempotency_key,
                    json.dumps(command),
                    cwd,
                    env_hash,
                    log_path,
                    started_at,
                ),
            )
        self.events.write(
            "run.started",
            task_id=task_id,
            run_id=run_id,
            payload={"command": command, "cwd": cwd},
        )

    def finish_run(
        self,
        *,
        run_id: str,
        exit_code: int,
        duration_ms: int,
        failure_kind: Optional[str],
        unmapped_exit: bool,
        evidence_path: Optional[str],
        manifest_path: Optional[str],
        finished_at: str,
    ) -> None:
        with transaction(self.conn):
            self.conn.execute(
                """
                UPDATE runs
                   SET exit_code=?, duration_ms=?, failure_kind=?, unmapped_exit=?,
                       evidence_path=?, manifest_path=?, finished_at=?
                 WHERE id=?;
                """,
                (
                    exit_code,
                    duration_ms,
                    failure_kind,
                    1 if unmapped_exit else 0,
                    evidence_path,
                    manifest_path,
                    finished_at,
                    run_id,
                ),
            )
        self.events.write(
            "run.finished",
            run_id=run_id,
            severity="info" if exit_code == 0 else "warning",
            payload={
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "failure_kind": failure_kind,
                "manifest_path": manifest_path,
            },
        )

    # ---- leases (process-level) ------------------------------------------

    def acquire_lease(self, owner: str, *, ttl_seconds: int) -> Dict[str, Any]:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        ts = now_iso()
        expires = _iso_plus_seconds(ts, ttl_seconds)
        record = {
            "owner": owner,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": ts,
            "expires_at": expires,
            "heartbeat_at": ts,
        }
        with transaction(self.conn):
            existing = self.conn.execute(
                "SELECT owner, pid, host, expires_at FROM leases WHERE owner=?;", (owner,)
            ).fetchone()
            if existing is not None and existing["expires_at"] > ts:
                raise RuntimeError(
                    f"lease '{owner}' is held by pid={existing['pid']} host={existing['host']} until {existing['expires_at']}"
                )
            self.conn.execute(
                """
                INSERT INTO leases(owner, pid, host, acquired_at, expires_at, heartbeat_at)
                VALUES (:owner, :pid, :host, :acquired_at, :expires_at, :heartbeat_at)
                ON CONFLICT(owner) DO UPDATE SET
                    pid=excluded.pid, host=excluded.host,
                    acquired_at=excluded.acquired_at,
                    expires_at=excluded.expires_at,
                    heartbeat_at=excluded.heartbeat_at;
                """,
                record,
            )
        self.paths.leases_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.paths.leases_dir / f"{owner}.json",
            record,
            indent=None,
            trailing_newline=False,
        )
        self.events.write("lease.acquired", payload=record)
        return record

    def heartbeat_lease(self, owner: str, *, ttl_seconds: int) -> str:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        ts = now_iso()
        expires = _iso_plus_seconds(ts, ttl_seconds)
        with transaction(self.conn):
            cur = self.conn.execute(
                """
                UPDATE leases
                   SET heartbeat_at=?, expires_at=?
                 WHERE owner=?;
                """,
                (ts, expires, owner),
            )
            if cur.rowcount == 0:
                raise KeyError(f"unknown lease owner: {owner}")
        self.events.write("lease.heartbeat", payload={"owner": owner, "expires_at": expires})
        return expires

    def release_lease(self, owner: str) -> None:
        with transaction(self.conn):
            self.conn.execute("DELETE FROM leases WHERE owner=?;", (owner,))
        marker = self.paths.leases_dir / f"{owner}.json"
        if marker.exists():
            marker.unlink()
        self.events.write("lease.released", payload={"owner": owner})

    # ---- recovery ---------------------------------------------------------

    def recovery_scan(self, *, stale_seconds: int = 90) -> Dict[str, Any]:
        if stale_seconds <= 0:
            raise ValueError("stale_seconds must be positive")
        self.events.write("recovery.scan_started", payload={})
        result: Dict[str, Any] = {
            "db_integrity": integrity_check(self.conn),
            "expired_leases": [],
            "active_expired_leases": [],
            "expired_task_leases": [],
            "abandoned_tasks": [],
        }
        ts = now_iso()
        stale_cutoff = _iso_minus_seconds(ts, stale_seconds)
        recovered_owners: set[str] = set()
        expired = self.conn.execute(
            "SELECT owner, pid, host, expires_at, heartbeat_at FROM leases WHERE expires_at <= ?;",
            (ts,),
        ).fetchall()
        for row in expired:
            if _lease_process_looks_alive(row) and row["heartbeat_at"] > stale_cutoff:
                self.events.write(
                    "recovery.lease_expired_but_active",
                    severity="warning",
                    payload={
                        "owner": row["owner"],
                        "pid": row["pid"],
                        "host": row["host"],
                        "expires_at": row["expires_at"],
                    },
                )
                result["active_expired_leases"].append(row["owner"])
                continue
            with transaction(self.conn):
                self.conn.execute("DELETE FROM leases WHERE owner=?;", (row["owner"],))
            self.events.write(
                "recovery.lease_expired",
                severity="warning",
                payload={"owner": row["owner"], "pid": row["pid"], "host": row["host"]},
            )
            result["expired_leases"].append(row["owner"])
            recovered_owners.add(row["owner"])

        leased = self.conn.execute(
            "SELECT id, lease_owner FROM tasks WHERE status='leased' AND (lease_expires IS NULL OR lease_expires <= ?);",
            (ts,),
        ).fetchall()
        for row in leased:
            if not self._task_owner_is_recoverable(row["lease_owner"], recovered_owners):
                continue
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE tasks SET status='queued', lease_owner=NULL, lease_expires=NULL, updated_at=? WHERE id=?;",
                    (ts, row["id"]),
                )
            self.events.write(
                "recovery.lease_expired",
                task_id=row["id"],
                severity="warning",
                payload={"prev_owner": row["lease_owner"]},
            )
            result["expired_task_leases"].append(row["id"])

        running = self.conn.execute(
            """
            SELECT id, lease_owner
              FROM tasks
             WHERE status='running'
               AND (lease_expires IS NULL OR lease_expires <= ?);
            """,
            (ts,),
        ).fetchall()
        for row in running:
            if not self._task_owner_is_recoverable(row["lease_owner"], recovered_owners):
                continue
            with transaction(self.conn):
                self.conn.execute(
                    """
                    UPDATE tasks
                       SET status='failed', error_class='abandoned', finished_at=?, updated_at=?,
                           lease_owner=NULL, lease_expires=NULL
                     WHERE id=?;
                    """,
                    (ts, ts, row["id"]),
                )
            self.events.write(
                "recovery.applied",
                task_id=row["id"],
                severity="warning",
                payload={"action": "marked_abandoned"},
            )
            result["abandoned_tasks"].append(row["id"])

        return result

    def _task_owner_is_recoverable(self, owner: Optional[str], recovered_owners: set[str]) -> bool:
        if owner is None:
            return True
        if owner in recovered_owners:
            return True
        active = self.conn.execute(
            "SELECT owner FROM leases WHERE owner=?;",
            (owner,),
        ).fetchone()
        return active is None


# ---- helpers ---------------------------------------------------------------

def _iso_plus_seconds(ts: str, seconds: int) -> str:
    base = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    new = base + timedelta(seconds=seconds)
    return new.strftime("%Y-%m-%dT%H:%M:%S.") + f"{new.microsecond // 1000:03d}Z"


def _iso_minus_seconds(ts: str, seconds: int) -> str:
    base = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    new = base - timedelta(seconds=seconds)
    return new.strftime("%Y-%m-%dT%H:%M:%S.") + f"{new.microsecond // 1000:03d}Z"


def _lease_process_looks_alive(row: sqlite3.Row) -> bool:
    if row["host"] != socket.gethostname():
        return False
    pid = int(row["pid"])
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def open_runtime(repo_root: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog, Orchestrator]:
    from .paths import runtime_paths_from_config

    paths = runtime_paths_from_config(repo_root)
    paths.ensure()
    conn = init_db(paths.db)
    # Issue #288 — migration v14 seeds the `default` project config-blind
    # (sut_root='.'). Reconcile it to the live sut.root now that config is
    # readable. Best-effort: a config read failure must never block runtime open.
    try:
        from .config import load_or_default
        from .projects import ensure_default_project

        cfg = load_or_default(repo_root)
        sut_root = (cfg.raw.get("sut") or {}).get("root") or "."
        ensure_default_project(conn, sut_root=str(sut_root))
    except Exception:
        pass
    # Codex PR #276 review (P2) — read `events.step_progress_throttle` from
    # config so the operator knob actually takes effect on the runtime path.
    events = event_log_for_paths(conn, paths)
    orchestrator = Orchestrator(conn, paths, events)
    orchestrator.seed_phases()
    return conn, paths, events, orchestrator


def fetch_task_summary(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM tasks GROUP BY status;"
    ).fetchall()
    summary = {"queued": 0, "leased": 0, "running": 0, "succeeded": 0, "failed": 0, "cancelled": 0, "timeout": 0}
    for row in rows:
        summary[row["status"]] = int(row["c"])
    return summary


def fetch_bug_summary(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM bugs GROUP BY status;"
    ).fetchall()
    summary = {"open": 0, "known": 0, "fixed": 0, "wont_fix": 0}
    for row in rows:
        summary[row["status"]] = int(row["c"])
    return summary


def fetch_phase_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, status, branch, started_at, finished_at FROM phases ORDER BY id;"
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_active_leases(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM leases ORDER BY owner;").fetchall()
    return [dict(r) for r in rows]


def fetch_last_run(
    conn: sqlite3.Connection,
    paths: Optional[RuntimePaths] = None,
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT id, task_id, exit_code, failure_kind, manifest_path, finished_at
          FROM runs
         WHERE finished_at IS NOT NULL
         ORDER BY finished_at DESC
        LIMIT 1;
        """
    ).fetchone()
    if row is None:
        return None
    payload = dict(row)
    if paths is not None:
        _attach_last_run_counts(paths, payload)
    return payload


def _attach_last_run_counts(paths: RuntimePaths, payload: Dict[str, Any]) -> None:
    """Attach `reports/last-run.json` counters when the latest run produced them."""
    payload["report_counts_available"] = False
    manifest_rel = payload.get("manifest_path")
    if not isinstance(manifest_rel, str) or not manifest_rel:
        return
    manifest_path = paths.repo_root / manifest_rel
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    payload["kind"] = manifest.get("kind")
    reports = manifest.get("reports") if isinstance(manifest, dict) else None
    if not isinstance(reports, dict) or not reports.get("finalized"):
        return
    reports_path = str(reports.get("path") or "reports")
    last_run_path = paths.repo_root / reports_path / "last-run.json"
    try:
        report = json.loads(last_run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(report, dict):
        return
    for key in ("total", "passed", "failed", "skipped"):
        value = report.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return
    failures = report.get("failures")
    if failures is not None and not isinstance(failures, list):
        return
    payload.update(
        {
            "total": report["total"],
            "passed": report["passed"],
            "failed": report["failed"],
            "skipped": report["skipped"],
            "failures": failures or [],
            "known_bug": _known_bug_failure_count(failures or []),
            "reports_path": reports_path,
            "report_counts_available": True,
        }
    )


def _known_bug_failure_count(failures: Iterable[Any]) -> int:
    total = 0
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        tags = failure.get("tags")
        if isinstance(tags, list) and any("known-bug" in str(tag).lower() for tag in tags):
            total += 1
    return total


def list_open_blockers(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, phase_id, severity, source, description FROM blockers WHERE status='open' ORDER BY severity, opened_at;"
    ).fetchall()
    return [dict(r) for r in rows]


def iter_phases() -> Iterable[tuple[str, str]]:
    return SEEDED_PHASES
