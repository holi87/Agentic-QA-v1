"""DecisionsMixin — extracted from routes/dashboard_server.py (issue #292)."""
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



class DecisionsMixin:
    """Methods grouped by domain; merged into ``_Handler`` via MRO."""

    def _decision_override(self, decision_id: str) -> None:
        """Issue #247 — operator override of an autonomous decision.

        Writes a new `actor='operator'` decision and links it back to the
        original via `decisions.reversed_by`. Write-gated.
        """
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        if not decision_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing_decision_id"})
            return
        try:
            body = self._read_optional_json_body() or {}
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        note = str(body.get("note") or "").strip()
        action = str(body.get("action") or "override").strip()
        if not note:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing_fields", "message": "note required"})
            return
        from ...decisions import record_decision
        from ...storage.db import transaction

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "db_missing"})
            return
        try:
            orig = conn.execute(
                "SELECT id, phase_id, topic FROM decisions WHERE id = ?;",
                (decision_id,),
            ).fetchone()
            if orig is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown_decision"})
                return
            new_id = record_decision(
                conn,
                phase_id=orig["phase_id"],
                topic=f"operator override: {orig['topic']}",
                actor="operator",
                rationale=f"[{action}] {note}",
                consequences=f"overrides decision {decision_id}",
            )
            with transaction(conn):
                conn.execute(
                    "UPDATE decisions SET reversed_by = ? WHERE id = ?;",
                    (new_id, decision_id),
                )
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, {"ok": True, "decision_id": new_id, "reversed": decision_id})

    def _serve_decisions(self, query: str) -> None:
        """Issue #247 — GET /api/decisions?limit=&actor=&before= decision log."""
        from urllib.parse import parse_qs

        from ...decisions import fetch_decisions

        params = parse_qs(query or "")
        actor = (params.get("actor") or [None])[0]
        before = (params.get("before") or [None])[0]
        try:
            limit = int((params.get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        try:
            rows = fetch_decisions(conn, limit=limit, actor=actor, before=before)
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, {"decisions": rows, "count": len(rows)})

    def _serve_decision(self, blocker_id: str) -> None:
        if not blocker_id:
            self._send_404()
            return
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_404()
            return
        try:
            detail = fetch_blocker_detail(conn, blocker_id)
        finally:
            conn.close()
        if detail is None:
            self._send_404()
            return
        self._send_json(HTTPStatus.OK, detail)
