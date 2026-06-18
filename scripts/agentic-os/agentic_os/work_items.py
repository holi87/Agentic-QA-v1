"""Operator-level work item intake for dashboard and CLI tasks."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .errors import InfraError, UsageError
from .events import EventLog
from .ids import ulid
from .paths import RuntimePaths
from .projects import DEFAULT_PROJECT_ID
from .runtime.tuning import MAX_SPEC_BYTES as _MAX_SPEC_BYTES
from .security import resolve_repo_path
from .storage.db import transaction
from .time_utils import now_iso


VALID_WORK_ITEM_STATUSES = {
    "draft",
    "queued",
    "analyzing",
    "planned",
    "implementing",
    "reviewing",
    "running",
    "bug_adjudication",
    "blocked",
    "done",
    "failed",
}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
_TRANSITIONS = {
    "draft": {"queued", "blocked", "failed"},
    "queued": {"analyzing", "planned", "implementing", "running", "blocked", "failed", "done"},
    "analyzing": {"planned", "blocked", "failed", "queued"},
    "planned": {"queued", "implementing", "blocked", "failed"},
    "implementing": {"reviewing", "running", "blocked", "failed"},
    "reviewing": {"running", "blocked", "failed", "done"},
    "running": {"bug_adjudication", "blocked", "done", "failed"},
    "bug_adjudication": {"queued", "blocked", "done", "failed"},
    "blocked": {"queued", "analyzing", "planned", "implementing", "reviewing", "running", "failed"},
    "failed": {"queued", "blocked"},
    "done": set(),
}
VALID_ARTIFACT_KINDS = {
    "spec",
    "sut_map",
    "analysis",
    "test_plan",
    "patch",
    "gate",
    # `apply` marks a patch that was actually applied to the working tree.
    # An APPROVE gate alone does not resolve a patch — see issue #87.
    "apply",
    "run",
    "bug",
    "report",
    "evidence",
}

_TITLE_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
_KEY_RE_TEMPLATE = r"^\s*{key}\s*:\s*(.+?)\s*$"


def create_work_item_from_file(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    source: Path,
    *,
    default_sut_root: str = ".",
    project_id: str = DEFAULT_PROJECT_ID,
) -> Dict[str, Any]:
    source_path = resolve_repo_path(paths.repo_root, str(source), label="task spec", must_exist=True)
    if not source_path.is_file():
        raise UsageError(f"task spec is not a file: {source}")
    content = _read_spec(source_path)
    title = _extract_title(content, fallback=source_path.stem)
    priority = _extract_key(content, "Priority", default="P2").upper()
    sut_root = _extract_key(content, "SUT root", default=default_sut_root)
    return _persist_work_item(
        conn,
        paths,
        events,
        title=title,
        priority=priority,
        sut_root=sut_root,
        spec_content=content,
        project_id=project_id,
    )


def create_work_item_from_payload(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    payload: Dict[str, Any],
    *,
    default_sut_root: str = ".",
    project_id: str = DEFAULT_PROJECT_ID,
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise UsageError("task payload must be a JSON object")
    title = _required_text(payload, "title", max_length=160)
    priority = str(payload.get("priority") or "P2").strip().upper()
    sut_root = _optional_text(payload, "sut_root", default=default_sut_root, max_length=300)
    # An explicit project_id in the payload overrides the caller default so the
    # dashboard/API can target a project per request.
    resolved_project_id = _optional_text(
        payload, "project_id", default=project_id, max_length=64
    )
    content = payload.get("spec_markdown")
    if content is not None:
        if not isinstance(content, str) or not content.strip():
            raise UsageError("spec_markdown must be a non-empty string")
        spec_content = content
    else:
        spec_content = render_task_spec_markdown(payload, title=title, priority=priority, sut_root=sut_root)
    return _persist_work_item(
        conn,
        paths,
        events,
        title=title,
        priority=priority,
        sut_root=sut_root,
        spec_content=spec_content,
        project_id=resolved_project_id,
    )


def render_task_spec_markdown(
    payload: Dict[str, Any],
    *,
    title: str,
    priority: str,
    sut_root: str,
) -> str:
    sections = [
        f"# {title}",
        "",
        f"Priority: {priority}",
        f"SUT root: {sut_root}",
        # Issue #290 — when an item is synthesized from a real signal the
        # planner-autopilot records the originating signal here so the
        # synthesis dedup (open-item spec grep) can recognise it later. A
        # human-created task omits ``source_signal`` and renders "(none)".
        f"Source signal: {_source_signal_field(payload.get('source_signal'))}",
        "",
        "## Business goal",
        _markdown_field(payload.get("business_goal")),
        "",
        "## Expected behavior",
        _markdown_field(payload.get("expected_behavior")),
        "",
        "## In scope",
        _markdown_field(payload.get("in_scope")),
        "",
        "## Out of scope",
        _markdown_field(payload.get("out_of_scope")),
        "",
        "## Known bugs",
        _markdown_field(payload.get("known_bugs")),
        "",
        "## Relevant endpoints or pages",
        _markdown_field(payload.get("relevant_surfaces")),
        "",
        "## Test data and credentials constraints",
        _markdown_field(payload.get("test_data")),
        "",
        "## Time budget",
        _markdown_field(payload.get("time_budget")),
        "",
    ]
    return "\n".join(sections)


def work_item_summary(conn: sqlite3.Connection) -> Dict[str, int]:
    """Aggregate operator work-item counts by status for the dashboard.

    Issue #191 — ``/api/status`` previously reported ``tasks.queued`` from
    the internal scheduler ``tasks`` table, which is unrelated to the
    operator queue surfaced by ``/api/tasks``. The Runtime card therefore
    showed ``Queued = 0`` while ``work_items`` still held queued rows.
    This helper exposes the real queue counters so the home card can
    render them and parity-test against ``list_work_items``.

    Every value in ``VALID_WORK_ITEM_STATUSES`` is present (zero when no
    rows match) so the dashboard JS never sees ``undefined``. ``total``
    is also returned for convenience — it equals ``len(list_work_items)``.
    """
    summary: Dict[str, int] = {status: 0 for status in VALID_WORK_ITEM_STATUSES}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM work_items GROUP BY status;"
    ).fetchall()
    total = 0
    unknown = 0
    for row in rows:
        status = row["status"]
        count = int(row["c"])
        if status not in VALID_WORK_ITEM_STATUSES:
            unknown += count
            total += count
            continue
        summary[status] += count
        total += count
    summary["unknown"] = unknown
    summary["total"] = total
    return summary


def list_work_items(
    conn: sqlite3.Connection, *, project_id: Optional[str] = None
) -> list[Dict[str, Any]]:
    """List work items, optionally scoped to one project.

    Issue #288 — ``project_id=None`` lists every work item (the zero-config
    single-SUT view), so existing callers are unchanged. Passing a
    ``project_id`` returns only that project's rows.
    """
    where = ""
    params: tuple[Any, ...] = ()
    if project_id is not None:
        where = "WHERE w.project_id = ?"
        params = (project_id,)
    rows = conn.execute(
        f"""
        SELECT w.id, w.title, w.status, w.spec_path, w.sut_root, w.project_id,
               w.priority, w.created_at, w.updated_at,
               a.kind AS last_artifact_kind,
               a.path AS last_artifact_path,
               a.created_at AS last_artifact_at
          FROM work_items AS w
          LEFT JOIN work_item_artifacts AS a
                 ON a.id = (
                      SELECT id FROM work_item_artifacts
                       WHERE work_item_id = w.id
                       ORDER BY created_at DESC, id DESC
                       LIMIT 1
                 )
         {where}
         ORDER BY w.created_at DESC, w.id DESC;
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def annotate_spec_status(paths: RuntimePaths, items: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Decorate each item with `spec_missing: bool` based on disk presence.

    The dashboard / CLI list endpoints call this so operators can spot orphan
    rows whose spec file was removed out-of-band (issue #49).

    Issue #192 — each decorated row also carries a ``candidate_debt``
    block (sourced from ``TEST-PLAN.json``) and a
    ``done_with_pending_decisions`` flag so the dashboard can render a
    warning chip on ``done`` tasks that still have undecided test
    candidates. The plan file is read once per row; tasks without a
    plan get a zeroed-out debt block so the UI never sees ``undefined``.
    """
    out: list[Dict[str, Any]] = []
    for item in items:
        decorated = dict(item)
        decorated["spec_missing"] = _spec_missing(paths, item.get("spec_path"))
        debt = compute_candidate_debt(paths, item.get("id"))
        decorated["candidate_debt"] = debt
        decorated["done_with_pending_decisions"] = (
            item.get("status") == "done"
            and int(debt.get("needs_operator_decision", 0) or 0) > 0
        )
        out.append(decorated)
    return out


# Issue #192 — every candidate decision is one of these. The dashboard
# warning chip fires when `needs_operator_decision > 0` on a `done` task;
# `not_testable` and `blocked_missing_docs` are explicit operator decisions
# (acceptance criteria #3) and do NOT count as debt.
_CANDIDATE_DECISION_KEYS = (
    "generate_now",
    "needs_operator_decision",
    "blocked_missing_docs",
    "not_testable",
)


def compute_candidate_debt(paths: RuntimePaths, work_item_id: Any) -> Dict[str, int]:
    """Read ``TEST-PLAN.json`` for a work item and return decision counts.

    Returns a zero-filled dict (every key from ``_CANDIDATE_DECISION_KEYS``
    plus ``total``) when the plan file is missing or unreadable — the
    dashboard always renders the block, never branches on its presence.
    """
    blank: Dict[str, int] = {key: 0 for key in _CANDIDATE_DECISION_KEYS}
    blank["total"] = 0
    if not isinstance(work_item_id, str) or not work_item_id:
        return blank
    plan_path = paths.runtime_root / "plans" / work_item_id / "TEST-PLAN.json"
    try:
        with plan_path.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return blank
    summary = payload.get("summary")
    items = payload.get("items") or []
    # Prefer the precomputed summary if present (written by
    # `summarize_plan` on every decision update). Fall back to counting
    # the items array so a hand-edited plan still surfaces honest debt.
    counts = dict(blank)
    if isinstance(summary, dict) and any(k in summary for k in _CANDIDATE_DECISION_KEYS):
        for key in _CANDIDATE_DECISION_KEYS:
            value = summary.get(key, 0)
            try:
                counts[key] = int(value or 0)
            except (TypeError, ValueError):
                counts[key] = 0
        try:
            counts["total"] = int(summary.get("total", sum(counts[k] for k in _CANDIDATE_DECISION_KEYS)))
        except (TypeError, ValueError):
            counts["total"] = sum(counts[k] for k in _CANDIDATE_DECISION_KEYS)
    else:
        for item in items:
            if not isinstance(item, dict):
                continue
            decision = item.get("decision")
            if decision in counts:
                counts[decision] += 1
        counts["total"] = len(items)
    return counts


def _spec_missing(paths: RuntimePaths, spec_path: Any) -> bool:
    if not isinstance(spec_path, str) or not spec_path:
        return True
    full = paths.repo_root / spec_path
    try:
        return not full.is_file()
    except OSError:
        return True


def prune_orphan_work_items(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    ids: Optional[Iterable[str]] = None,
) -> list[Dict[str, Any]]:
    """Delete work_items rows whose spec_path no longer exists on disk.

    When `ids` is provided, only those rows are considered (still required to
    have a missing spec — the function refuses to delete rows whose spec is
    still on disk). Cascades to work_item_artifacts via ON DELETE CASCADE.
    Returns the list of pruned rows as `{id, title, spec_path}` dicts.
    """
    candidate_ids = set(ids) if ids is not None else None
    pruned: list[Dict[str, Any]] = []
    with transaction(conn):
        rows = conn.execute(
            "SELECT id, title, spec_path FROM work_items;"
        ).fetchall()
        for row in rows:
            row_dict = dict(row)
            if candidate_ids is not None and row_dict["id"] not in candidate_ids:
                continue
            if not _spec_missing(paths, row_dict["spec_path"]):
                continue
            conn.execute("DELETE FROM work_items WHERE id=?;", (row_dict["id"],))
            pruned.append(row_dict)
    if pruned:
        events.write(
            "work_item.pruned",
            actor="operator",
            payload={
                "count": len(pruned),
                "ids": [r["id"] for r in pruned],
            },
        )
    return pruned


def delete_work_item(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    work_item_id: str,
    reason: str | None = None,
) -> Dict[str, Any]:
    """Delete a single work item and its runtime artifacts (issue #224).

    The spec markdown file in `pretask/` is preserved on disk — only the DB
    row, the `work_item_artifacts` cascade rows and the runtime directories
    under `agentic-os-runtime/{plans,patches,runs,evidence,task-specs}/<id>/`
    are removed. Emits `work_item.deleted` for audit.
    """
    _require_work_item_id(work_item_id)
    row = conn.execute(
        "SELECT id, title, spec_path FROM work_items WHERE id=?;",
        (work_item_id,),
    ).fetchone()
    if row is None:
        raise UsageError(f"unknown task id: {work_item_id}")
    row_dict = dict(row)
    marker = paths.tmp_dir / f"{work_item_id}.pending_delete"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"work_item_id": work_item_id, "reason": reason or ""}), encoding="utf-8")
    runtime_dirs = [
        paths.runtime_root / sub / work_item_id
        for sub in ("plans", "patches", "runs", "evidence", "task-specs")
    ]
    removed_paths: list[str] = []
    for directory in runtime_dirs:
        try:
            if directory.exists():
                _remove_tree(directory)
                removed_paths.append(str(directory.relative_to(paths.repo_root)))
        except OSError as exc:
            marker.write_text(
                json.dumps({"work_item_id": work_item_id, "error": str(exc)}),
                encoding="utf-8",
            )
            raise InfraError(f"delete failed for {directory}: {exc}") from exc
    with transaction(conn):
        conn.execute("DELETE FROM work_items WHERE id=?;", (work_item_id,))
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    events.write(
        "work_item.deleted",
        actor="operator",
        payload={
            "work_item_id": work_item_id,
            "title": row_dict["title"],
            "spec_path": row_dict["spec_path"],
            "removed_runtime_paths": removed_paths,
            "reason": reason or "",
        },
    )
    return {
        "work_item_id": work_item_id,
        "removed_runtime_paths": removed_paths,
    }


def _remove_tree(path: Path) -> None:
    """Recursively remove ``path``. Imported lazily to avoid a top-level
    ``shutil`` import for a single call site."""
    import shutil

    shutil.rmtree(path)


def link_work_items(
    conn: sqlite3.Connection,
    events: EventLog,
    *,
    parent_id: str,
    child_id: str,
    kind: str = "blocks",
) -> Dict[str, Any]:
    """Record a dependency edge: ``parent`` must finish before ``child`` runs.

    Issue #274 — the DEPENDENCY/HYBRID queue policies will not return ``child``
    as the next work item until ``parent`` reaches ``done``. The edge is
    idempotent (``INSERT OR IGNORE`` on the composite PK). Both ids must exist
    and differ; a direct self-cycle is rejected.
    """
    if parent_id == child_id:
        raise UsageError("a work item cannot depend on itself")
    ts = now_iso()
    with transaction(conn):
        if get_work_item(conn, parent_id) is None:
            raise UsageError(f"unknown parent task id: {parent_id}")
        if get_work_item(conn, child_id) is None:
            raise UsageError(f"unknown child task id: {child_id}")
        conn.execute(
            """
            INSERT OR IGNORE INTO work_item_deps(parent_id, child_id, kind, created_at)
            VALUES (?, ?, ?, ?);
            """,
            (parent_id, child_id, kind, ts),
        )
    edge = {"parent_id": parent_id, "child_id": child_id, "kind": kind}
    events.write(
        "work_item.linked",
        actor="operator",
        payload=edge,
    )
    return edge


def list_work_item_deps(conn: sqlite3.Connection) -> list[Dict[str, Any]]:
    """Return all dependency edges ordered by child then parent."""
    rows = conn.execute(
        """
        SELECT parent_id, child_id, kind, created_at
          FROM work_item_deps
         ORDER BY child_id ASC, parent_id ASC;
        """
    ).fetchall()
    return [dict(row) for row in rows]


def get_work_item(conn: sqlite3.Connection, work_item_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT id, title, status, spec_path, sut_root, project_id, priority,
               created_at, updated_at
          FROM work_items
         WHERE id=?;
        """,
        (work_item_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def list_work_item_artifacts(conn: sqlite3.Connection, work_item_id: str) -> list[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, work_item_id, kind, path, created_at
          FROM work_item_artifacts
         WHERE work_item_id=?
         ORDER BY created_at ASC, id ASC;
        """,
        (work_item_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_work_item_detail(conn: sqlite3.Connection, work_item_id: str) -> Optional[Dict[str, Any]]:
    work_item = get_work_item(conn, work_item_id)
    if work_item is None:
        return None
    return {
        "work_item": work_item,
        "artifacts": list_work_item_artifacts(conn, work_item_id),
    }


def register_work_item_artifact(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    work_item_id: str,
    kind: str,
    path: str,
) -> Dict[str, Any]:
    _require_work_item_id(work_item_id)
    if kind not in VALID_ARTIFACT_KINDS:
        raise UsageError(f"invalid artifact kind: {kind}")
    resolved = resolve_repo_path(paths.repo_root, path, label="artifact path", must_exist=False)
    rel_path = _rel(paths, resolved)
    artifact_id = ulid()
    ts = now_iso()
    with transaction(conn):
        if get_work_item(conn, work_item_id) is None:
            raise UsageError(f"unknown task id: {work_item_id}")
        conn.execute(
            """
            INSERT INTO work_item_artifacts(id, work_item_id, kind, path, created_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (artifact_id, work_item_id, kind, rel_path, ts),
        )
    artifact = {
        "id": artifact_id,
        "work_item_id": work_item_id,
        "kind": kind,
        "path": rel_path,
        "created_at": ts,
    }
    events.write(
        "work_item.artifact_registered",
        actor="operator",
        payload=artifact,
    )
    return artifact


def read_work_item_spec(paths: RuntimePaths, work_item: Dict[str, Any]) -> str:
    spec_path = work_item.get("spec_path")
    if not spec_path:
        raise UsageError("work item has no spec_path")
    resolved = resolve_repo_path(paths.repo_root, spec_path, label="spec_path", must_exist=True)
    if not resolved.is_file():
        raise UsageError(f"spec_path is not a file: {spec_path}")
    return resolved.read_text(encoding="utf-8")


def update_work_item_status(
    conn: sqlite3.Connection,
    events: EventLog,
    *,
    work_item_id: str,
    status: str,
) -> Dict[str, Any]:
    if status not in VALID_WORK_ITEM_STATUSES:
        raise UsageError(f"invalid status: {status}")
    ts = now_iso()
    with transaction(conn):
        existing = get_work_item(conn, work_item_id)
        if existing is None:
            raise UsageError(f"unknown task id: {work_item_id}")
        _validate_transition(existing["status"], status)
        if status in {"running", "done", "bug_adjudication", "failed"}:
            _require_approved_gates_for_patches(
                conn,
                events,
                work_item_id=work_item_id,
                next_status=status,
            )
        conn.execute(
            "UPDATE work_items SET status=?, updated_at=? WHERE id=?;",
            (status, ts, work_item_id),
        )
    events.write(
        "work_item.status_changed",
        actor="operator",
        payload={
            "work_item_id": work_item_id,
            "from": existing["status"],
            "to": status,
        },
    )
    updated = get_work_item(conn, work_item_id)
    assert updated is not None
    return updated


def validate_task_runtime_inputs(paths: RuntimePaths, *, sut_root: str) -> str:
    resolved = resolve_repo_path(paths.repo_root, sut_root, label="sut_root", must_exist=False)
    return _rel(paths, resolved)


def _require_approved_gates_for_patches(
    conn: sqlite3.Connection,
    events: EventLog,
    *,
    work_item_id: str,
    next_status: str,
) -> None:
    paths = events.paths
    from .gates import find_patch_gate_violations

    violations = find_patch_gate_violations(paths, conn=conn, work_item_id=work_item_id)
    if not violations:
        return
    details = "; ".join(f"{v.path}: {v.message}" for v in violations[:3])
    raise UsageError(
        f"approved review gate required before task {work_item_id} can be {next_status}: {details}"
    )


def _persist_work_item(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    title: str,
    priority: str,
    sut_root: str,
    spec_content: str,
    project_id: str = DEFAULT_PROJECT_ID,
) -> Dict[str, Any]:
    title = _clean_text(title, field="title", max_length=160)
    if priority not in VALID_PRIORITIES:
        raise UsageError(f"priority must be one of {sorted(VALID_PRIORITIES)}")
    sut_root_rel = validate_task_runtime_inputs(paths, sut_root=sut_root)
    encoded = spec_content.encode("utf-8")
    if not encoded or len(encoded) > _MAX_SPEC_BYTES:
        raise UsageError(f"task spec must be between 1 and {_MAX_SPEC_BYTES} bytes")
    work_item_id = _allocate_work_item_id(title)
    paths.task_specs_dir.mkdir(parents=True, exist_ok=True)
    dest = paths.task_specs_dir / f"{work_item_id}.md"
    tmp = dest.with_name(dest.name + f".{ulid()}.tmp")
    rel_spec = _rel(paths, dest)
    ts = now_iso()
    try:
        tmp.write_text(_normalize_markdown(spec_content), encoding="utf-8")
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO work_items(
                    id, title, status, spec_path, sut_root, project_id,
                    priority, created_at, updated_at)
                VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?);
                """,
                (work_item_id, title, rel_spec, sut_root_rel, project_id, priority, ts, ts),
            )
            conn.execute(
                """
                INSERT INTO work_item_artifacts(id, work_item_id, kind, path, created_at)
                VALUES (?, ?, 'spec', ?, ?);
                """,
                (ulid(), work_item_id, rel_spec, ts),
            )
            os.replace(tmp, dest)
    except Exception:  # intentionally broad: cleanup-then-reraise — any failure must unlink the temp/dest spec files before propagating the original error
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        raise
    events.write(
        "work_item.created",
        actor="operator",
        payload={
            "work_item_id": work_item_id,
            "title": title,
            "priority": priority,
            "spec_path": rel_spec,
        },
    )
    detail = get_work_item_detail(conn, work_item_id)
    if detail is None:
        raise InfraError(f"work item was not persisted: {work_item_id}")
    return detail


def _read_spec(path: Path) -> str:
    data = path.read_bytes()
    if not data or len(data) > _MAX_SPEC_BYTES:
        raise UsageError(f"task spec must be between 1 and {_MAX_SPEC_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError("task spec must be UTF-8 Markdown") from exc


def _extract_title(content: str, *, fallback: str) -> str:
    match = _TITLE_RE.search(content)
    if match:
        return _clean_text(match.group(1), field="title", max_length=160)
    return _clean_text(fallback.replace("-", " "), field="title", max_length=160)


def _extract_key(content: str, key: str, *, default: str) -> str:
    pattern = re.compile(_KEY_RE_TEMPLATE.format(key=re.escape(key)), re.IGNORECASE | re.MULTILINE)
    match = pattern.search(content)
    if not match:
        return default
    return _clean_text(match.group(1), field=key, max_length=300)


def _allocate_work_item_id(title: str) -> str:
    ts = now_iso()
    return f"TASK-{ts[0:10].replace('-', '')}-{ts[11:19].replace(':', '')}-{ulid().lower()}-{_slugify(title)}"


def _slugify(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text.strip().lower()).strip("-")
    return slug[:48] or "task"


def _normalize_markdown(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    if "\x00" in normalized:
        raise UsageError("task spec contains a NUL byte")
    return normalized


def _clean_text(value: Any, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise UsageError(f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise UsageError(f"{field} must not be empty")
    if "\x00" in cleaned:
        raise UsageError(f"{field} contains a NUL byte")
    if len(cleaned) > max_length:
        raise UsageError(f"{field} must be <= {max_length} characters")
    return cleaned


def _required_text(payload: Dict[str, Any], key: str, *, max_length: int) -> str:
    return _clean_text(payload.get(key), field=key, max_length=max_length)


def _optional_text(payload: Dict[str, Any], key: str, *, default: str, max_length: int) -> str:
    value = payload.get(key)
    if value is None:
        return default
    return _clean_text(value, field=key, max_length=max_length)


def _source_signal_field(value: Any) -> str:
    """Render the issue #290 synthesis dedup token onto a single spec line.

    Kept on one ``Source signal: <token>`` line (not a section body) so the
    synthesis dedup grep can extract the exact token verbatim. Absent →
    "(none)", which the dedup extractor treats as no signal.
    """
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "(none)"


def _markdown_field(value: Any) -> str:
    if value is None or value == "":
        return "TBD"
    if isinstance(value, str):
        return value.strip() or "TBD"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    if isinstance(value, Iterable):
        return "\n".join(f"- {str(item).strip()}" for item in value if str(item).strip()) or "TBD"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _rel(paths: RuntimePaths, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(paths.repo_root.resolve()))
    except ValueError as exc:
        raise UsageError(f"path escapes repo root: {path}") from exc


def _require_work_item_id(work_item_id: str) -> None:
    if not re.fullmatch(r"TASK-[0-9]{8}-[0-9]{6}-[a-z0-9][a-z0-9-]*", work_item_id):
        raise UsageError(f"invalid task id: {work_item_id}")


def _validate_transition(current: str, next_status: str) -> None:
    if current == next_status:
        return
    allowed = _TRANSITIONS.get(current, set())
    if next_status not in allowed:
        raise UsageError(f"invalid work item status transition: {current} -> {next_status}")
