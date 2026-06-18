"""Issue #319 (Wave 12) — persistent per-SUT/project coverage ledger.

A durable record of every surface a generator has covered: the route/endpoint
(``surface_key``), the kind of assertion exercised (``assertion_kind``), the
spec file that covers it, and the run/work item that produced it. It answers
"is surface X already covered?" so #320 can gate idempotent accumulation on it
(re-running an unchanged SUT must add zero duplicate specs).

The ledger reuses #288/#289 per-project scoping: every row carries a
``project_id`` so SUTs/projects never mix. Recording is idempotent — a surface
re-generated in a later run updates the pointer to the newest spec/run instead
of inserting a duplicate row.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .atomic_io import atomic_write_json
from .errors import UsageError
from .ids import ulid
from .time_utils import now_iso

SURFACE_KINDS = ("api", "ui")

# Assertion-kind buckets, derived from the plan item's free-text expectation.
# Coarse on purpose: the ledger records *what kind* of check covers a surface,
# not the exact wording, so cosmetic rephrasing does not fork a new row.
_API_STATUS_RE = re.compile(r"\bstatus\b|\b[1-5]\d{2}\b", re.IGNORECASE)
_API_SCHEMA_RE = re.compile(r"\b(json|schema|body|field|payload)\b", re.IGNORECASE)
_UI_VISIBLE_RE = re.compile(r"\b(visible|see|seen|display|shown|render)\w*\b", re.IGNORECASE)


def surface_key(
    test_type: str,
    *,
    target_method: Optional[str] = None,
    target_path: Optional[str] = None,
    target_page: Optional[str] = None,
) -> str:
    """Canonical, stable identifier for the surface a test covers.

    API surfaces are ``"<METHOD> <path>"`` (e.g. ``"GET /users"``); UI surfaces
    are the target page/route verbatim (e.g. ``"/login"``).
    """
    if test_type == "api":
        method = (target_method or "").strip().upper()
        path = (target_path or "").strip()
        if not method or not path:
            raise UsageError("api surface_key requires target_method and target_path")
        return f"{method} {path}"
    if test_type == "ui":
        page = (target_page or "").strip()
        if not page:
            raise UsageError("ui surface_key requires target_page")
        return page
    raise UsageError(f"unsupported test_type for surface_key: {test_type!r}")


def classify_assertion(test_type: str, expected_assertion: str) -> str:
    """Bucket a free-text expectation into a coarse, deterministic kind."""
    text = expected_assertion or ""
    if test_type == "api":
        if _API_STATUS_RE.search(text):
            return "status"
        if _API_SCHEMA_RE.search(text):
            return "schema"
        return "business"
    if test_type == "ui":
        if _UI_VISIBLE_RE.search(text):
            return "visible"
        return "business"
    raise UsageError(f"unsupported test_type for classify_assertion: {test_type!r}")


def record_coverage(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    surface_kind: str,
    surface_key: str,
    assertion_kind: str,
    spec_path: str,
    candidate_id: Optional[str] = None,
    work_item_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> str:
    """Insert (or refresh) one covered-surface row; returns the row id.

    Idempotent on ``(project_id, surface_kind, surface_key, assertion_kind)``:
    re-recording the same surface updates the spec/run pointer and
    ``updated_at`` instead of inserting a duplicate.
    """
    if surface_kind not in SURFACE_KINDS:
        raise UsageError(f"unsupported surface_kind: {surface_kind!r}")
    now = now_iso()
    existing = conn.execute(
        """
        SELECT id FROM coverage_ledger
         WHERE project_id=? AND surface_kind=? AND surface_key=? AND assertion_kind=?;
        """,
        (project_id, surface_kind, surface_key, assertion_kind),
    ).fetchone()
    if existing is not None:
        row_id = existing["id"]
        conn.execute(
            """
            UPDATE coverage_ledger
               SET spec_path=?, candidate_id=?, work_item_id=?, run_id=?, updated_at=?
             WHERE id=?;
            """,
            (spec_path, candidate_id, work_item_id, run_id, now, row_id),
        )
        return row_id
    row_id = ulid()
    conn.execute(
        """
        INSERT INTO coverage_ledger(
          id, project_id, surface_kind, surface_key, assertion_kind,
          spec_path, candidate_id, work_item_id, run_id, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?);
        """,
        (
            row_id,
            project_id,
            surface_kind,
            surface_key,
            assertion_kind,
            spec_path,
            candidate_id,
            work_item_id,
            run_id,
            now,
            now,
        ),
    )
    return row_id


@dataclass
class _GeneratedSpec:
    candidate_id: str
    relative_path: str


def record_generated_coverage(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    plan_items: Iterable[Any],
    generated_tests: Sequence[Any],
    run_id: Optional[str] = None,
    work_item_id: Optional[str] = None,
) -> List[str]:
    """Record coverage for every spec a generation run emitted.

    Maps each generated spec back to its plan item by ``candidate_id`` to derive
    the surface and assertion kind, then records one ledger row per spec.
    Items whose generated counterpart is missing are skipped silently — the
    generators already enforced which items produced specs.
    """
    by_candidate: Dict[str, Any] = {
        getattr(item, "candidate_id", ""): item for item in plan_items
    }
    recorded: List[str] = []
    for gen in generated_tests:
        candidate_id = getattr(gen, "candidate_id", "")
        item = by_candidate.get(candidate_id)
        if item is None:
            continue
        test_type = getattr(item, "test_type", "")
        try:
            key = surface_key(
                test_type,
                target_method=getattr(item, "target_method", None),
                target_path=getattr(item, "target_path", None),
                target_page=getattr(item, "target_page", None),
            )
        except UsageError:
            continue
        recorded.append(
            record_coverage(
                conn,
                project_id=project_id,
                surface_kind=test_type,
                surface_key=key,
                assertion_kind=classify_assertion(
                    test_type, getattr(item, "expected_assertion", "")
                ),
                spec_path=getattr(gen, "relative_path", ""),
                candidate_id=candidate_id or None,
                work_item_id=work_item_id,
                run_id=run_id,
            )
        )
    return recorded


def is_covered(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    surface_kind: str,
    surface_key: str,
    assertion_kind: Optional[str] = None,
) -> bool:
    """True if the project already covers this surface.

    Without ``assertion_kind`` any recorded assertion on the surface counts as a
    hit; with it, only that specific assertion kind does.
    """
    if assertion_kind is None:
        row = conn.execute(
            """
            SELECT 1 FROM coverage_ledger
             WHERE project_id=? AND surface_kind=? AND surface_key=? LIMIT 1;
            """,
            (project_id, surface_kind, surface_key),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT 1 FROM coverage_ledger
             WHERE project_id=? AND surface_kind=? AND surface_key=? AND assertion_kind=?
             LIMIT 1;
            """,
            (project_id, surface_kind, surface_key, assertion_kind),
        ).fetchone()
    return row is not None


def find_existing_spec(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    surface_kind: str,
    surface_key: str,
) -> Optional[str]:
    """Return the most recently recorded ``spec_path`` for a covered surface.

    Issue #329 — the in-place extension path needs to locate the file that
    already covers a surface so a new assertion bucket can be appended
    instead of emitting a sibling spec. Returns ``None`` when the surface
    has no prior coverage; the caller falls back to the new-file path.
    Any assertion_kind on the surface counts — rows recorded by the extend
    path share the same ``spec_path``, and rows recorded before this change
    still resolve to the original generated file.
    """
    row = conn.execute(
        """
        SELECT spec_path FROM coverage_ledger
         WHERE project_id=? AND surface_kind=? AND surface_key=?
         ORDER BY updated_at DESC
         LIMIT 1;
        """,
        (project_id, surface_kind, surface_key),
    ).fetchone()
    if row is None:
        return None
    spec_path = row["spec_path"]
    if isinstance(spec_path, str) and spec_path:
        return spec_path
    return None


def partition_by_coverage(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    plan_items: Iterable[Any],
) -> tuple[List[Any], List[Dict[str, Any]]]:
    """Split plan items into the (delta, skipped) the generator should act on.

    Issue #320 — an item whose surface + assertion bucket already lives in the
    project's ledger is *skipped* (its spec already exists); everything else is
    the *delta* to generate. Items without a derivable surface cannot be keyed,
    so they stay in the delta and let the generator validate/reject them.

    ``skipped`` rows carry the surface identity and an ``already_covered``
    reason so the caller can emit auditable events.
    """
    delta: List[Any] = []
    skipped: List[Dict[str, Any]] = []
    for item in plan_items:
        test_type = getattr(item, "test_type", "")
        try:
            key = surface_key(
                test_type,
                target_method=getattr(item, "target_method", None),
                target_path=getattr(item, "target_path", None),
                target_page=getattr(item, "target_page", None),
            )
        except UsageError:
            delta.append(item)
            continue
        assertion_kind = classify_assertion(
            test_type, getattr(item, "expected_assertion", "")
        )
        if is_covered(
            conn,
            project_id=project_id,
            surface_kind=test_type,
            surface_key=key,
            assertion_kind=assertion_kind,
        ):
            skipped.append(
                {
                    "candidate_id": getattr(item, "candidate_id", None),
                    "surface_kind": test_type,
                    "surface_key": key,
                    "assertion_kind": assertion_kind,
                    "reason": "already_covered",
                }
            )
        else:
            delta.append(item)
    return delta, skipped


def build_coverage_entries(
    plan_items: Iterable[Any],
    generated_tests: Sequence[Any],
    *,
    project_id: str,
    work_item_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Pure helper: turn (PlanItems, GeneratedTests) into JSON-ready ledger rows.

    Used at generation time to write a pending-coverage manifest beside the
    patch; the apply step ingests that manifest into the DB once `git apply`
    actually lands the spec files (Codex P1 — never record before apply).
    Items whose generated spec is missing or whose surface cannot be derived
    are skipped silently.
    """
    by_candidate: Dict[str, Any] = {
        getattr(item, "candidate_id", ""): item for item in plan_items
    }
    entries: List[Dict[str, Any]] = []
    for gen in generated_tests:
        candidate_id = getattr(gen, "candidate_id", "")
        item = by_candidate.get(candidate_id)
        if item is None:
            continue
        test_type = getattr(item, "test_type", "")
        try:
            key = surface_key(
                test_type,
                target_method=getattr(item, "target_method", None),
                target_path=getattr(item, "target_path", None),
                target_page=getattr(item, "target_page", None),
            )
        except UsageError:
            continue
        entries.append(
            {
                "project_id": project_id,
                "surface_kind": test_type,
                "surface_key": key,
                "assertion_kind": classify_assertion(
                    test_type, getattr(item, "expected_assertion", "")
                ),
                "spec_path": getattr(gen, "relative_path", ""),
                "candidate_id": candidate_id or None,
                "work_item_id": work_item_id,
                "run_id": run_id,
            }
        )
    return entries


def write_pending_manifest(path: Path, entries: Sequence[Dict[str, Any]]) -> None:
    """Atomic write of the pending-coverage manifest beside the patch file."""
    atomic_write_json(Path(path), {"version": "1.0", "entries": list(entries)})


def ingest_pending_manifest(conn: sqlite3.Connection, path: Path) -> int:
    """Apply-time ingest: record every entry from the manifest into the ledger.

    Idempotent — re-ingesting the same manifest hits ``record_coverage``'s
    upsert path and produces no new rows. A missing manifest is treated as
    "nothing to ingest" so older patches without one don't break apply.
    Returns the number of entries processed.
    """
    p = Path(path)
    if not p.is_file():
        return 0
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        record_coverage(
            conn,
            project_id=entry["project_id"],
            surface_kind=entry["surface_kind"],
            surface_key=entry["surface_key"],
            assertion_kind=entry["assertion_kind"],
            spec_path=entry["spec_path"],
            candidate_id=entry.get("candidate_id"),
            work_item_id=entry.get("work_item_id"),
            run_id=entry.get("run_id"),
        )
    return len(entries)


def list_coverage(conn: sqlite3.Connection, *, project_id: str) -> List[Dict[str, Any]]:
    """Every covered surface for a project, newest-recorded first."""
    rows = conn.execute(
        """
        SELECT id, project_id, surface_kind, surface_key, assertion_kind,
               spec_path, candidate_id, work_item_id, run_id, created_at, updated_at
          FROM coverage_ledger
         WHERE project_id=?
         ORDER BY updated_at DESC, surface_kind ASC, surface_key ASC;
        """,
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]
