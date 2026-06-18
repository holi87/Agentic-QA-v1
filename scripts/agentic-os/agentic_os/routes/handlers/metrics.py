"""MetricsMixin — extracted from routes/dashboard_server.py (issue #292)."""
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



class MetricsMixin:
    """Methods grouped by domain; merged into ``_Handler`` via MRO."""

    def _serve_status(self) -> None:
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.OK, {
                "runtime": "blocked", "db": "missing", "current_phase": CURRENT_PHASE_ID,
                "leases": [], "phases": [], "tasks": {}, "work_items": {}, "bugs": {},
                "last_run": None, "blockers_open": [],
            })
            return
        try:
            self._send_json(HTTPStatus.OK, build_status(conn, self.paths))
        finally:
            conn.close()

    def _serve_dashboard_overview(self) -> None:
        """Issue #193 — cockpit aggregations (work items, candidates,
        generated tests, latest run, current process, next action)."""
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.OK, {
                "work_items": {}, "candidates": {}, "generated_tests": {},
                "latest_run": None, "current_process": None, "next_action": None,
                "bugs": {}, "blockers": {},
            })
            return
        try:
            self._send_json(HTTPStatus.OK, build_overview(conn, self.paths))
        finally:
            conn.close()

    def _serve_dashboard_preflight(self) -> None:
        """Issue #199 — readiness checklist used by the home page."""
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.OK, {
                "ok": False, "warn": False,
                "checks": [{
                    "id": "runtime_db",
                    "status": "fail",
                    "message": "runtime DB missing — initialize with `./scripts/agentic-os.sh up`.",
                    "actions": ["Run `./scripts/agentic-os.sh up` to bootstrap the runtime DB."],
                }],
            })
            return
        try:
            write_enabled, _ = _dashboard_write_settings(self.paths)
            self._send_json(
                HTTPStatus.OK,
                build_preflight(conn, self.paths, write_enabled=write_enabled),
            )
        finally:
            conn.close()

    def _serve_metrics_json(self) -> None:
        """Issue #314 (Wave 14) — unified metrics rollup for the cockpit.

        Drops back to an empty rollup when the DB is missing so the
        cockpit page can still render against a fresh install.
        """
        from ...metrics import build_metrics

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(
                HTTPStatus.OK,
                {
                    "generated_at": now_iso(),
                    "sessions": {"recent": [], "totals": {"sessions": 0, "blocks": 0, "failures": 0}},
                    "tests": {"work_items_total": 0, "outcomes": {}, "runs_recent": []},
                    "coverage": {"by_project": [], "by_surface_kind": {}, "total_rows": 0},
                    "cost": {"totals": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "invocations": 0}},
                    "providers": {"active_cooldowns": [], "failover_events": 0},
                    "blocks": {"total_blocks": 0, "by_reason": []},
                    "phase_timing": {"by_kind": []},
                },
            )
            return
        try:
            self._send_json(HTTPStatus.OK, build_metrics(conn, self.paths))
        finally:
            conn.close()

    def _serve_metrics_prometheus(self) -> None:
        """Issue #314 (Wave 14) — Prometheus exposition format.

        Sibling of ``/api/metrics`` that reuses the same rollup; the
        ``Content-Type`` follows the standard Prometheus exposition
        ``text/plain; version=0.0.4`` so a generic Prometheus scrape
        config does not need extra header tweaks.
        """
        from ...metrics import build_metrics, render_prometheus

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            payload = ""
        else:
            try:
                payload = render_prometheus(build_metrics(conn, self.paths))
            finally:
                conn.close()
        data = payload.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_dashboard_charts(self) -> None:
        """Issue #195 — pre-aggregated chart data (no remote deps)."""
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.OK, {
                "run_history": [], "failure_trend": {}, "funnel": {},
                "bugs": {}, "blockers": {},
            })
            return
        try:
            self._send_json(HTTPStatus.OK, build_charts(conn, self.paths))
        finally:
            conn.close()

    def _serve_health_repair(self) -> None:
        """Dry-run repair report for the /health page (read-only)."""
        from ... import repair as _repair

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(
                HTTPStatus.OK,
                {"total": 0, "findings": [], "counts": {}, "safe_count": 0, "hard_count": 0},
            )
            return
        try:
            report = _repair.build_report(conn, self.paths)
        finally:
            conn.close()
        report["write_enabled"] = _dashboard_write_settings(self.paths)[0]
        self._send_json(HTTPStatus.OK, report)

    def _health_repair_apply_action(self) -> None:
        """Apply repairs (write-gated). Body may carry `safe_only: bool`."""
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
        safe_only = bool(isinstance(body, dict) and body.get("safe_only"))
        from ... import repair as _repair

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "db_missing", "message": "runtime database not initialised"},
            )
            return
        try:
            events = event_log_for_paths(conn, self.paths)
            result = _repair.repair(conn, self.paths, events, apply=True, safe_only=safe_only)
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, {"ok": True, **result})

    def _serve_budget_status(self, query: str) -> None:
        """GET /api/budget/status — live token/USD consumption vs limits."""
        from urllib.parse import parse_qs

        from ...budgets import budget_status
        from ...config import load_or_default

        params = parse_qs(query or "")
        session_id = (params.get("session") or [None])[0]
        try:
            cfg = load_or_default(self.paths.repo_root)
            budgets = (cfg.raw.get("budgets") or {}) if isinstance(cfg.raw, dict) else {}
            models = (cfg.raw.get("models") or {}) if isinstance(cfg.raw, dict) else {}
        except ConfigError:
            budgets = {}
            models = {}
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        try:
            payload = budget_status(conn, budgets, session_id=session_id, models=models)
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, payload)

    def _serve_provider_cooldowns(self, query: str) -> None:
        """GET /api/providers/cooldowns — active provider cooldown windows."""
        from urllib.parse import parse_qs

        from ...models.failover import active_cooldowns

        params = parse_qs(query or "")
        role = (params.get("role") or [None])[0]
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "db_missing"})
            return
        try:
            rows = active_cooldowns(conn, role=role)
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, {"cooldowns": rows})
