"""SessionsMixin — extracted from routes/dashboard_server.py (issue #292)."""
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



class SessionsMixin:
    """Methods grouped by domain; merged into ``_Handler`` via MRO."""

    def _serve_sessions_list(self, query: str) -> None:
        """GET /api/sessions?limit=&offset=&mode=&status=&actor= — history index."""
        from urllib.parse import parse_qs

        from ...sessions import list_sessions

        params = parse_qs(query or "")

        def _q(name):
            return (params.get(name) or [None])[0]

        try:
            limit = int(_q("limit") or 50)
            offset = int(_q("offset") or 0)
        except (TypeError, ValueError):
            limit, offset = 50, 0
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        try:
            rows = list_sessions(
                conn, limit=limit, offset=offset,
                mode=_q("mode"), status=_q("status"), actor=_q("actor"),
            )
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, {"sessions": rows, "count": len(rows)})

    def _serve_session_detail(self, session_id: str) -> None:
        from ...sessions import get_session

        session_id = session_id.strip("/")
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        try:
            sess = get_session(conn, session_id)
        finally:
            conn.close()
        if sess is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "session_not_found"})
            return
        # Issue #272 — surface the PR-ready handoff doc when it exists so the
        # detail page can link to it (served read-only via `/files/...`).
        from ...summaries import summary_relpath

        relpath = summary_relpath(session_id)
        if (self.paths.repo_root / relpath).is_file():
            sess["summary_path"] = relpath
        self._send_json(HTTPStatus.OK, {"session": sess})

    def _serve_sessions_compare(self, query: str) -> None:
        from urllib.parse import parse_qs

        from ...sessions import compare_sessions

        params = parse_qs(query or "")
        a = (params.get("a") or [None])[0]
        b = (params.get("b") or [None])[0]
        if not a or not b:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "a and b required"})
            return
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        try:
            payload = compare_sessions(conn, a, b)
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, payload)

    def _session_bookmark_action(self, session_id: str) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        try:
            payload = self._read_optional_json_body() or {}
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        label = str((payload or {}).get("label", "")) if isinstance(payload, dict) else ""
        from ...sessions import set_bookmark
        from ...storage.db import transaction

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        try:
            with transaction(conn):
                ok = set_bookmark(conn, session_id, label)
        finally:
            conn.close()
        if not ok:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "session_not_found"})
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "session_id": session_id, "label": label})

    def _serve_learnings_list(self, query: str) -> None:
        """GET /api/learnings?kind=&subject=&limit=&offset= — store index (#273)."""
        from urllib.parse import parse_qs

        from ...learnings import list_learnings

        params = parse_qs(query or "")

        def _q(name):
            return (params.get(name) or [None])[0]

        try:
            limit = int(_q("limit") or 100)
            offset = int(_q("offset") or 0)
        except (TypeError, ValueError):
            limit, offset = 100, 0
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        try:
            rows = list_learnings(
                conn, kind=_q("kind"), subject=_q("subject"), limit=limit, offset=offset
            )
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, {"learnings": rows, "count": len(rows)})

    def _serve_learning_detail(self, learning_id: str) -> None:
        from ...learnings import get_learning

        learning_id = learning_id.strip("/")
        try:
            lid = int(learning_id)
        except (TypeError, ValueError):
            self._send_404()
            return
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        try:
            row = get_learning(conn, lid)
        finally:
            conn.close()
        if row is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "learning_not_found"})
            return
        self._send_json(HTTPStatus.OK, {"learning": row})

    def _learning_forget_action(self, learning_id: str) -> None:
        """POST /api/learnings/<id>/forget — operator override (#273)."""
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        try:
            lid = int(learning_id.strip("/"))
        except (TypeError, ValueError):
            self._send_404()
            return
        from ...learnings import forget_learning

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        try:
            ok = forget_learning(conn, lid)
        finally:
            conn.close()
        if not ok:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "learning_not_found"})
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "forgotten": lid})

    def _serve_transcript(self, invocation_id: str) -> None:
        """GET /api/transcripts/<invocation_id> — structured reasoning trail."""
        from ...transcripts import get_transcript

        invocation_id = invocation_id.strip("/")
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        try:
            chunks = get_transcript(conn, invocation_id)
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, {"invocation_id": invocation_id, "chunks": chunks})
