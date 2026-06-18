"""Self-healing repair scan for the runtime (issue #274).

`agentic-os doctor --repair` detects and (optionally) repairs five classes of
runtime drift that accumulate when a daemon crashes, a delete is interrupted,
or files are removed out of band:

  * ``stale_lease``        — a ``leases`` row whose owning process is dead.
  * ``orphan_spec``        — a task-spec file on disk with no work_items row.
  * ``missing_ndjson``     — an ``event_offsets`` consumer pointing at an
                             NDJSON file that no longer exists.
  * ``partial_autocommit`` — a work item is ``done`` yet the SUT branch still
                             has uncommitted changes from its patch.
  * ``pending_delete``     — a ``<id>.pending_delete`` marker left behind by an
                             interrupted ``delete_work_item`` (the rmtree never
                             completed).

SAFETY CONTRACT (deliberate, see issue #274):
  * There are NO interactive prompts — the scan is safe in non-tty and
    autonomous contexts.
  * ``apply=False`` (the default / ``--dry-run``) NEVER mutates anything; it
    only reports what *would* be repaired.
  * ``apply=True`` (``--yes``) performs the repairs.

Repairs are partitioned into ``safe`` and ``hard``:
  * SAFE   — clearing stale leases and deleting orphan spec files. These are
             idempotent and lossless, so ``up --auto-repair`` may apply them
             unattended.
  * HARD   — everything else (DB row mutations, completing a delete). These
             require an explicit ``doctor --repair --yes``.
"""
from __future__ import annotations

import os
import socket
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .events import EventLog
from .paths import RuntimePaths

# Repair classes that `up --auto-repair` is allowed to apply unattended.
SAFE_CLASSES = ("stale_lease", "orphan_spec")


def _process_alive(pid: int, host: str) -> bool:
    """True when ``pid`` on ``host`` still looks like a live local process.

    Mirrors ``orchestrator._lease_process_looks_alive`` but takes plain values
    so it works on dicts as well as sqlite rows. A lease owned by another host
    is treated as not-locally-alive (we cannot signal it), which is the
    conservative choice for *clearing* a stale lease on this machine.
    """
    if host != socket.gethostname():
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _detect_stale_leases(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT owner, pid, host FROM leases ORDER BY owner;"
        ).fetchall()
    except sqlite3.Error:
        return findings
    for row in rows:
        if _process_alive(int(row["pid"]), str(row["host"])):
            continue
        findings.append(
            {
                "class": "stale_lease",
                "id": row["owner"],
                "detail": f"owner={row['owner']} pid={row['pid']} host={row['host']} (process not alive)",
                "safe": True,
            }
        )
    return findings


def _detect_orphan_specs(
    conn: sqlite3.Connection, paths: RuntimePaths
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    specs_dir = paths.task_specs_dir
    if not specs_dir.exists():
        return findings
    try:
        known = {
            row["spec_path"]
            for row in conn.execute("SELECT spec_path FROM work_items;").fetchall()
        }
    except sqlite3.Error:
        known = set()
    # Resolve known spec paths to absolutes for comparison.
    known_abs = {str((paths.repo_root / p).resolve()) for p in known if p}
    for spec_file in sorted(specs_dir.rglob("*")):
        if not spec_file.is_file():
            continue
        if str(spec_file.resolve()) in known_abs:
            continue
        findings.append(
            {
                "class": "orphan_spec",
                "id": spec_file.name,
                "detail": f"task-spec file with no work_items row: {spec_file}",
                "path": str(spec_file),
                "safe": True,
            }
        )
    return findings


def _detect_missing_ndjson(
    conn: sqlite3.Connection, paths: RuntimePaths
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT consumer, ndjson_file FROM event_offsets;"
        ).fetchall()
    except sqlite3.Error:
        return findings
    for row in rows:
        rel = row["ndjson_file"]
        if not rel:
            continue
        candidate = Path(rel)
        if not candidate.is_absolute():
            candidate = paths.events_dir / rel
        if candidate.exists():
            continue
        findings.append(
            {
                "class": "missing_ndjson",
                "id": row["consumer"],
                "detail": f"event consumer '{row['consumer']}' references missing NDJSON file: {rel}",
                "safe": False,
            }
        )
    return findings


def _detect_partial_autocommit(
    conn: sqlite3.Connection, paths: RuntimePaths
) -> List[Dict[str, Any]]:
    """Flag work items marked ``done`` whose SUT branch is dirty.

    Best-effort and read-only: we shell out to ``git status --porcelain`` in
    each distinct ``sut_root``. A repo we cannot inspect is simply skipped — we
    never want the doctor to crash on an exotic checkout. The "repair" for this
    class is advisory only (no auto-fix); applying it just records the event so
    operators see it in the audit log.
    """
    import subprocess

    findings: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT id, sut_root FROM work_items WHERE status='done';"
        ).fetchall()
    except sqlite3.Error:
        return findings
    # Group by sut_root so we run git once per repo.
    by_root: Dict[str, List[str]] = {}
    for row in rows:
        root = row["sut_root"] or "."
        by_root.setdefault(root, []).append(row["id"])
    for root, ids in by_root.items():
        repo = (paths.repo_root / root).resolve()
        if not repo.exists():
            continue
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        if not proc.stdout.strip():
            continue
        for wid in ids:
            findings.append(
                {
                    "class": "partial_autocommit",
                    "id": wid,
                    "detail": (
                        f"work item {wid} is 'done' but SUT branch at {root} "
                        f"has uncommitted changes"
                    ),
                    "safe": False,
                }
            )
    return findings


def _detect_pending_delete(paths: RuntimePaths) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    tmp_dir = paths.tmp_dir
    if not tmp_dir.exists():
        return findings
    for marker in sorted(tmp_dir.glob("*.pending_delete")):
        wid = marker.name[: -len(".pending_delete")]
        # Leftover runtime dirs the interrupted rmtree never finished.
        leftover = [
            paths.runtime_root / sub / wid
            for sub in ("plans", "patches", "runs", "evidence", "task-specs")
            if (paths.runtime_root / sub / wid).exists()
        ]
        findings.append(
            {
                "class": "pending_delete",
                "id": wid,
                "detail": (
                    f"pending_delete marker for {wid} with "
                    f"{len(leftover)} undeleted runtime dir(s)"
                ),
                "marker": str(marker),
                "leftover": [str(p) for p in leftover],
                "safe": False,
            }
        )
    return findings


def detect(conn: sqlite3.Connection, paths: RuntimePaths) -> List[Dict[str, Any]]:
    """Return all repair findings (read-only)."""
    findings: List[Dict[str, Any]] = []
    findings.extend(_detect_stale_leases(conn))
    findings.extend(_detect_orphan_specs(conn, paths))
    findings.extend(_detect_missing_ndjson(conn, paths))
    findings.extend(_detect_partial_autocommit(conn, paths))
    findings.extend(_detect_pending_delete(paths))
    return findings


def build_report(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    *,
    safe_only: bool = False,
) -> Dict[str, Any]:
    """Build the dry-run repair report payload.

    ``safe_only`` filters the findings to the classes ``up --auto-repair`` may
    act on, used to preview the startup sweep.
    """
    findings = detect(conn, paths)
    if safe_only:
        findings = [f for f in findings if f.get("safe")]
    counts: Dict[str, int] = {}
    for f in findings:
        counts[f["class"]] = counts.get(f["class"], 0) + 1
    return {
        "findings": findings,
        "counts": counts,
        "total": len(findings),
        "safe_count": sum(1 for f in findings if f.get("safe")),
        "hard_count": sum(1 for f in findings if not f.get("safe")),
    }


def _apply_one(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    finding: Dict[str, Any],
) -> bool:
    """Apply a single repair. Returns True when an action was taken."""
    import shutil

    cls = finding["class"]
    applied = False
    if cls == "stale_lease":
        conn.execute("DELETE FROM leases WHERE owner=?;", (finding["id"],))
        conn.commit()
        marker = paths.leases_dir / f"{finding['id']}.json"
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        applied = True
    elif cls == "orphan_spec":
        path = Path(finding["path"])
        try:
            path.unlink()
            applied = True
        except FileNotFoundError:
            applied = False
    elif cls == "missing_ndjson":
        conn.execute(
            "DELETE FROM event_offsets WHERE consumer=?;", (finding["id"],)
        )
        conn.commit()
        applied = True
    elif cls == "pending_delete":
        for leftover in finding.get("leftover", []):
            p = Path(leftover)
            try:
                if p.exists():
                    shutil.rmtree(p)
            except OSError:
                pass
        try:
            Path(finding["marker"]).unlink()
        except FileNotFoundError:
            pass
        applied = True
    elif cls == "partial_autocommit":
        # Advisory only — we never auto-commit/discard SUT work. Emit a
        # *detected* event (not *applied*) so audit-log readers are not misled
        # into thinking a mutation happened. Reported but never counted as an
        # applied repair.
        events.write(
            "doctor.repair.detected",
            actor="operator",
            severity="warning",
            payload={"class": cls, "id": finding["id"], "detail": finding.get("detail", "")},
        )
        return False
    if applied:
        events.write(
            "doctor.repair.applied",
            actor="operator",
            payload={"class": cls, "id": finding["id"], "detail": finding.get("detail", "")},
        )
    return applied


def repair(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    apply: bool = False,
    safe_only: bool = False,
    classes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Detect and (when ``apply``) repair runtime drift.

    ``apply=False`` is a pure dry-run — nothing is mutated. ``safe_only`` limits
    repairs to :data:`SAFE_CLASSES` (used by ``up --auto-repair``). ``classes``
    optionally restricts to specific finding classes.

    Returns the report payload extended with ``applied`` (the list of repairs
    actually performed; empty in dry-run) and ``dry_run``.
    """
    findings = detect(conn, paths)
    if safe_only:
        findings = [f for f in findings if f["class"] in SAFE_CLASSES]
    if classes is not None:
        wanted = set(classes)
        findings = [f for f in findings if f["class"] in wanted]

    applied: List[Dict[str, Any]] = []
    if apply:
        for finding in findings:
            if _apply_one(conn, paths, events, finding):
                applied.append(
                    {"class": finding["class"], "id": finding["id"]}
                )

    counts: Dict[str, int] = {}
    for f in findings:
        counts[f["class"]] = counts.get(f["class"], 0) + 1
    return {
        "dry_run": not apply,
        "findings": findings,
        "counts": counts,
        "total": len(findings),
        "safe_count": sum(1 for f in findings if f.get("safe")),
        "hard_count": sum(1 for f in findings if not f.get("safe")),
        "applied": applied,
    }
