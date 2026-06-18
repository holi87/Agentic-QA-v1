"""AutonomyMixin — extracted from routes/dashboard_server.py (issue #292)."""
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



class AutonomyMixin:
    """Methods grouped by domain; merged into ``_Handler`` via MRO."""

    def _serve_schedules(self) -> None:
        from ...scheduler import list_schedules

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.OK, {"schedules": []})
            return
        try:
            schedules = [s.as_dict() for s in list_schedules(conn)]
        finally:
            conn.close()
        self._send_json(
            HTTPStatus.OK,
            {"schedules": schedules, "write_enabled": _dashboard_write_settings(self.paths)[0]},
        )

    def _create_schedule_action(self) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled", "message": _WRITE_DISABLED_MSG},
            )
            return
        try:
            body = self._read_optional_json_body()
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        if not isinstance(body, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_body", "message": "JSON object required"},
            )
            return
        name = body.get("name")
        cron = body.get("cron")
        action = body.get("action")
        if not all(isinstance(v, str) and v.strip() for v in (name, cron, action)):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "missing_fields", "message": "name, cron and action are required strings"},
            )
            return
        from ...scheduler import CronError, add_schedule

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "db_missing", "message": "runtime database not initialised"},
            )
            return
        try:
            try:
                sched = add_schedule(
                    conn,
                    name=name,
                    cron=cron,
                    action=action,
                    enabled=bool(body.get("enabled", True)),
                )
            except CronError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_cron", "message": str(exc)},
                )
                return
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, {"ok": True, "schedule": sched.as_dict()})

    def _schedule_toggle_action(self, name: str, *, enable: bool) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled", "message": _WRITE_DISABLED_MSG},
            )
            return
        from ...scheduler import set_enabled

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_404()
            return
        try:
            updated = set_enabled(conn, name, enable)
        finally:
            conn.close()
        if not updated:
            self._send_404()
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "name": name, "enabled": enable})

    def _schedule_run_now_action(self, name: str) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled", "message": _WRITE_DISABLED_MSG},
            )
            return
        from ...scheduler import get_schedule, run_now

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_404()
            return
        try:
            if get_schedule(conn, name) is None:
                self._send_404()
                return
            events = event_log_for_paths(conn, self.paths)
            payload = run_now(conn, events, self.paths, name)
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, {"ok": True, "fired": payload})

    def _delete_schedule_action(self, name: str) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled", "message": _WRITE_DISABLED_MSG},
            )
            return
        from ...scheduler import remove_schedule

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_404()
            return
        try:
            removed = remove_schedule(conn, name)
        finally:
            conn.close()
        if not removed:
            self._send_404()
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "removed": name})

    def _runtime_recovery_action(self) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        from ...orchestrator import Orchestrator
        from ...workflows import run_recovery

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "db_missing"})
            return
        events = event_log_for_paths(conn, self.paths)
        orch = Orchestrator(conn, self.paths, events)
        try:
            result = run_recovery(orch, self.paths, events)
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, result.to_json())

    def _autonomy_start_action(self) -> None:
        """POST /api/autonomy/start — kick off a full-autonomy session."""
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        try:
            payload = self._read_optional_json_body() or {}
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        if not isinstance(payload, dict):
            payload = {}
        max_minutes = payload.get("max_minutes", 60)
        try:
            max_minutes = int(max_minutes)
        except (TypeError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "max_minutes must be int"})
            return
        if max_minutes < 15 or max_minutes > 720:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "max_minutes must be between 15 and 720"},
            )
            return
        from ...autonomy import start_session
        session = start_session(self.paths, max_minutes=max_minutes)
        # under_one_hour warning surfaces in payload so the UI can show banner.
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "session_id": session.session_id,
                "max_minutes": max_minutes,
                "started_at": session.started_at,
                "expected_finish_at": session.expected_finish_at,
                "under_one_hour": max_minutes < 60,
            },
        )

    def _autonomy_stop_action(self) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        from ...autonomy import stop_session
        status = stop_session()
        self._send_json(HTTPStatus.OK, {"ok": True, "status": status})

    def _autonomy_pause_resume_action(self, action: str) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        from ...autonomy import pause_session, resume_session
        status = pause_session() if action == "pause" else resume_session()
        self._send_json(HTTPStatus.OK, {"ok": True, "action": action, "status": status})

    def _serve_autonomy_status(self) -> None:
        from ...autonomy import current_status
        status = current_status()
        self._send_json(HTTPStatus.OK, status)

    def _serve_autonomy_preflight(self) -> None:
        from ...autonomy import preflight_check
        try:
            payload = preflight_check(self.paths)
        except Exception as exc:  # intentionally broad: mirrors AutonomyManager._preflight — any failure becomes a structured "fail" check, never a 500
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": False,
                    "checks": [{
                        "id": "preflight",
                        "status": "fail",
                        "message": f"preflight raised: {exc}",
                        "actions": ["Inspect the dashboard server log."],
                    }],
                },
            )
            return
        self._send_json(HTTPStatus.OK, payload)
