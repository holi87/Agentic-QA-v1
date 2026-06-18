"""EventsMixin — extracted from routes/dashboard_server.py (issue #292)."""
from __future__ import annotations

import json
import contextvars
import hmac
import os
import secrets
import shutil
import sqlite3
import threading
import time
from html import escape as html_escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from ...config import ConfigError, load_or_default
from ...errors import UsageError
from ...events import EventLog, event_log_for_paths
from ...orchestrator import (
    CURRENT_PHASE_ID,
    Orchestrator,
    fetch_active_leases,
    fetch_bug_summary,
    fetch_last_run,
    fetch_phase_rows,
    fetch_task_summary,
    list_open_blockers,
)
from ...workflows import (
    WorkflowResult,
    run_final_gate,
    run_review_gate,
    run_tests,
)
from ...paths import RuntimePaths
from ...time_utils import now_iso
from ...storage.db import connect, integrity_check
from ...work_items import (
    annotate_spec_status,
    compute_candidate_debt,
    create_work_item_from_payload,
    delete_work_item,
    get_work_item,
    get_work_item_detail,
    list_work_item_artifacts,
    list_work_items,
    prune_orphan_work_items,
    read_work_item_spec,
    work_item_summary,
)
from ...dashboard import build_charts, build_overview, build_preflight
from ...analysis import analyze_work_item
from ...patch_builder import implement_tests_for_work_item
from ...security import redact_sensitive_text, resolve_repo_path
from ...runtime.tuning import MAX_JSON_BODY_BYTES as _MAX_JSON_BODY_BYTES
from .._dispatch import RouteDispatcher
from ...test_planning import (
    plan_work_item,
    read_plan_candidates,
    update_plan_candidate_decision,
)
from .._dashboard_state import (  # noqa: F401
    DEFAULT_HOST,
    DEFAULT_PORT,
    NAV_ACTIVE,
    NAV_LINKS,
    NAV_SENTINEL,
    STATIC_CONTENT_TYPES,
    STATIC_DIR,
    TEMPLATES_DIR,
    _ACTION_ORDER,
    _ALLOWED_LOCAL_HOSTS,
    _CONFIG_WRITE_DISABLED_MSG,
    _FULL_MODE_OVERRIDE,
    _FULL_MODE_OVERRIDE_EVENT,
    _ROUTES,
    _SSE_KEEPALIVE_SECONDS,
    _SSE_POLL_SECONDS,
    _WRITE_DISABLED_MSG,
    _autonomy_writes_active,
    _compute_action_gating,
    _content_type_header,
    _dashboard_config_write_settings,
    _dashboard_write_settings,
    _is_under,
    _load_or_create_dashboard_token,
    _open_db,
    _parse_json,
    _parse_kind_filter,
    _retention_sweep_on_startup,
    _workflow_payload,
    build_status,
    fetch_blocker_detail,
    fetch_coverage_state,
    fetch_task_detail,
    generated_tests_for_work_item,
    is_full_mode_active,
    render_nav,
    set_full_mode_override,
)



class EventsMixin:
    """Methods grouped by domain; merged into ``_Handler`` via MRO."""

    def _serve_events_sse(self) -> None:
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        # Issue #245 — optional `?kind=step.*` (glob) and `?kind=step.start` (exact)
        # filters so the orchestration view subscribes only to step.* without
        # forcing every client to do JS-side filtering.
        kind_filter = _parse_kind_filter(self.path)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        last_id: Optional[str] = None
        last_keepalive = time.monotonic()
        try:
            while not getattr(self.server, "_shutdown_requested", False):
                rows = self._poll_events(conn, last_id)
                if rows:
                    for row in rows:
                        last_id = row["id"]
                        if kind_filter is not None and not kind_filter(row["kind"]):
                            continue
                        payload = {
                            "id": row["id"],
                            "ts": row["ts"],
                            "kind": row["kind"],
                            "severity": row["severity"],
                            "actor": row["actor"],
                            "payload": _parse_json(row["payload"]),
                            "phase_id": row["phase_id"],
                            "task_id": row["task_id"],
                            "run_id": row["run_id"],
                        }
                        self._sse_send(f"id: {row['id']}\ndata: {json.dumps(payload, sort_keys=True)}\n\n")
                else:
                    if time.monotonic() - last_keepalive > _SSE_KEEPALIVE_SECONDS:
                        self._sse_send(": keepalive\n\n")
                        last_keepalive = time.monotonic()
                time.sleep(_SSE_POLL_SECONDS)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            conn.close()

    def _serve_events_history(self, query: str) -> None:
        """Issue #246 — paginated JSON event history.

        `GET /api/events/history?before=<id>&kind=step.*&limit=<n>` returns
        the most recent events with id < before (descending), optionally
        filtered by the same `kind` glob the SSE stream uses. Used by the
        orchestration timeline's "Load more" control.
        """
        from urllib.parse import parse_qs

        params = parse_qs(query or "")
        before = (params.get("before") or [None])[0]
        # Issue #269 — replay reuses NDJSON: a [from_ts, to_ts] window scopes
        # the event history to a single past session's lifespan.
        from_ts = (params.get("from") or [None])[0]
        to_ts = (params.get("to") or [None])[0]
        try:
            limit = int((params.get("limit") or ["200"])[0])
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 500))
        kind_filter = _parse_kind_filter("?" + (query or ""))
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        clauses: list = []
        sql_params: list = []
        if before:
            clauses.append("id < ?")
            sql_params.append(before)
        if from_ts:
            clauses.append("ts >= ?")
            sql_params.append(from_ts)
        if to_ts:
            clauses.append("ts <= ?")
            sql_params.append(to_ts)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql_params.append(limit * 4 if kind_filter else limit)
        try:
            rows = conn.execute(
                "SELECT id, ts, kind, severity, actor, payload, phase_id, task_id, run_id "
                f"FROM events{where} ORDER BY id DESC LIMIT ?;",
                sql_params,
            ).fetchall()
        finally:
            conn.close()
        out = []
        for row in rows:
            if kind_filter is not None and not kind_filter(row["kind"]):
                continue
            out.append({
                "id": row["id"],
                "ts": row["ts"],
                "kind": row["kind"],
                "severity": row["severity"],
                "actor": row["actor"],
                "payload": _parse_json(row["payload"]),
                "phase_id": row["phase_id"],
                "task_id": row["task_id"],
                "run_id": row["run_id"],
            })
            if len(out) >= limit:
                break
        self._send_json(HTTPStatus.OK, {"events": out, "count": len(out)})

    def _poll_events(self, conn: sqlite3.Connection, after_id: Optional[str]) -> list:
        if after_id is None:
            rows = conn.execute(
                "SELECT id, ts, kind, severity, actor, payload, phase_id, task_id, run_id "
                "FROM events ORDER BY ts DESC, id DESC LIMIT 20;"
            ).fetchall()
            return list(reversed(rows))
        return list(
            conn.execute(
                "SELECT id, ts, kind, severity, actor, payload, phase_id, task_id, run_id "
                "FROM events WHERE id > ? ORDER BY id ASC LIMIT 100;",
                (after_id,),
            ).fetchall()
        )

    def _sse_send(self, chunk: str) -> None:
        try:
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise
