"""Issue #288 — addressable projects over the flat work_items list.

The runtime was single-SUT-per-checkout. This module turns projects into
first-class, addressable rows so work items (and, via #289, RAG memory) can be
scoped by ``project_id``. A literal ``default`` project always exists (seeded by
migration v14 / ``schema.sql``); its ``sut_root`` is reconciled from the live
config at runtime, keeping the migration itself config-blind.

Read-side filtering stays opt-in: callers that pass no ``project_id`` see every
row, so existing single-SUT behaviour is unchanged.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Any, Dict, List, Optional

from .errors import UsageError
from .time_utils import now_iso

DEFAULT_PROJECT_ID = "default"

# Registered project ids are slug-like so they read well in CLI/config and on
# disk. The reserved literal ``default`` is always valid.
_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "sut_root": row["sut_root"],
        "config_ref": row["config_ref"],
        "created_at": row["created_at"],
    }


def _slugify(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text.strip().lower()).strip("-")
    return slug[:64]


def get_project(conn: sqlite3.Connection, project_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT id, name, sut_root, config_ref, created_at FROM projects WHERE id=?;",
        (project_id,),
    ).fetchone()
    return _row_to_dict(row) if row is not None else None


def list_projects(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, name, sut_root, config_ref, created_at
          FROM projects
         ORDER BY created_at ASC, id ASC;
        """
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def register_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    sut_root: str,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Register a new addressable project.

    The id defaults to a slug of ``name`` when not given. Duplicate ids are
    rejected so ``register`` is a deliberate create, never a silent upsert —
    use :func:`ensure_default_project` for the reconcile path.
    """
    if not isinstance(name, str) or not name.strip():
        raise UsageError("project name must be a non-empty string")
    if not isinstance(sut_root, str) or not sut_root.strip():
        raise UsageError("project sut_root must be a non-empty string")
    pid = (project_id or _slugify(name)).strip()
    if not _PROJECT_ID_RE.fullmatch(pid):
        raise UsageError(
            "project id must be lowercase slug [a-z0-9-], 1-64 chars: " + repr(pid)
        )
    ts = now_iso()
    row = {
        "id": pid,
        "name": name.strip(),
        "sut_root": sut_root.strip(),
        "config_ref": None,
        "created_at": ts,
    }
    try:
        conn.execute(
            """
            INSERT INTO projects(id, name, sut_root, config_ref, created_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (row["id"], row["name"], row["sut_root"], row["config_ref"], row["created_at"]),
        )
    except sqlite3.IntegrityError as exc:
        raise UsageError(f"project already exists: {pid}") from exc
    return row


def ensure_default_project(
    conn: sqlite3.Connection,
    *,
    sut_root: str,
    name: str = "default",
) -> Dict[str, Any]:
    """Guarantee the ``default`` project exists and mirrors the live config.

    Migration v14 seeds ``default`` with ``sut_root='.'`` because migrations are
    config-blind. At runtime the active ``sut.root`` is known, so this reconciles
    the row. Idempotent: safe to call on every runtime open.
    """
    resolved_root = sut_root.strip() if isinstance(sut_root, str) and sut_root.strip() else "."
    conn.execute(
        """
        INSERT INTO projects(id, name, sut_root, config_ref, created_at)
        VALUES (?, ?, ?, NULL, ?)
        ON CONFLICT(id) DO UPDATE SET sut_root=excluded.sut_root, name=excluded.name;
        """,
        (DEFAULT_PROJECT_ID, name, resolved_root, now_iso()),
    )
    project = get_project(conn, DEFAULT_PROJECT_ID)
    assert project is not None
    return project


def resolve_active_project_id(
    conn: sqlite3.Connection,
    cfg: Any = None,
    *,
    explicit: Optional[str] = None,
) -> str:
    """Resolve the active project id by precedence: explicit > config > default.

    An explicit flag or a configured ``project.active`` that names a project
    which does not exist is an operator error (``UsageError``). The ``default``
    project always exists, so the no-config path never raises.
    """
    if explicit is not None:
        pid = explicit.strip()
        if get_project(conn, pid) is None:
            raise UsageError(f"unknown project: {pid}")
        return pid
    configured = _config_active(cfg)
    if configured is not None:
        if get_project(conn, configured) is None:
            raise UsageError(f"config project.active names an unknown project: {configured}")
        return configured
    return DEFAULT_PROJECT_ID


def _config_active(cfg: Any) -> Optional[str]:
    raw = getattr(cfg, "raw", None)
    if not isinstance(raw, dict):
        return None
    project = raw.get("project")
    if not isinstance(project, dict):
        return None
    active = project.get("active")
    if isinstance(active, str) and active.strip():
        return active.strip()
    return None
