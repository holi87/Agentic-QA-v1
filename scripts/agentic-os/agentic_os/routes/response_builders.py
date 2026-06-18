"""ResponseBuildersMixin — extracted from routes/dashboard_server.py (issue #292)."""
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

from ..config import ConfigError, load_or_default
from ..errors import UsageError
from ..events import EventLog, event_log_for_paths
from ..orchestrator import (
    CURRENT_PHASE_ID,
    Orchestrator,
    fetch_active_leases,
    fetch_bug_summary,
    fetch_last_run,
    fetch_phase_rows,
    fetch_task_summary,
    list_open_blockers,
)
from ..workflows import (
    WorkflowResult,
    run_final_gate,
    run_review_gate,
    run_tests,
)
from ..paths import RuntimePaths
from ..time_utils import now_iso
from ..storage.db import connect, integrity_check
from ..work_items import (
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
from ..dashboard import build_charts, build_overview, build_preflight
from ..analysis import analyze_work_item
from ..patch_builder import implement_tests_for_work_item
from ..security import redact_sensitive_text, resolve_repo_path
from ..runtime.tuning import MAX_JSON_BODY_BYTES as _MAX_JSON_BODY_BYTES
from ._dispatch import RouteDispatcher
from ..test_planning import (
    plan_work_item,
    read_plan_candidates,
    update_plan_candidate_decision,
)
from ._dashboard_state import (  # noqa: F401
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



class ResponseBuildersMixin:
    """Methods grouped by domain; merged into ``_Handler`` via MRO."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Keep stderr clean; orchestrator EventLog is the audit log.
        return

    def _send_json(self, status: int, body: Dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, status: int, body: str, content_type: str = "text/plain") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_template(self, name: str) -> None:
        path = TEMPLATES_DIR / name
        if not path.exists():
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, f"missing template: {name}")
            return
        body = path.read_text(encoding="utf-8")
        body = self._inject_nav(body, name)
        body = self._inject_dashboard_token(body)
        self._send_text(HTTPStatus.OK, body, content_type="text/html")

    def _inject_nav(self, html: str, name: str) -> str:
        """Replace the nav sentinel with the canonical nav (issue #296/#321).

        Every served template — including the task/decision detail views
        (#321) — carries ``NAV_SENTINEL`` and gets the one canonical nav
        injected here. Templates without the sentinel are left untouched.
        """
        if NAV_SENTINEL not in html:
            return html
        return html.replace(NAV_SENTINEL, render_nav(NAV_ACTIVE.get(name)))

    def _inject_dashboard_token(self, html: str) -> str:
        """Embed the unsafe-method token + a fetch shim into served HTML.

        Issue #291 — rather than editing every per-page `fetch()` call site,
        wrap `window.fetch` once so any POST/PUT/DELETE to a same-origin path
        carries `X-Agentic-Token`. Reading it cross-origin is blocked by CORS,
        so this stays a CSRF-safe synchroniser token.

        The generated token is `token_urlsafe` ([A-Za-z0-9_-]), but an
        operator-supplied `AGENTIC_DASHBOARD_TOKEN` / `.dashboard_token` may
        contain `"` or `</script>`. Escape both embed contexts so an arbitrary
        token cannot break out of the attribute or the JS string literal.
        """
        token = self.dashboard_token
        if not token or "<head>" not in html:
            return html
        attr_token = html_escape(token, quote=True)
        # JS string literal: json.dumps handles quotes/backslashes; the extra
        # replacements neutralise an HTML-context `</script>` (and `<!--`)
        # breakout inside the literal.
        js_token = (
            json.dumps(token)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )
        snippet = (
            f'<head>\n  <meta name="agentic-dashboard-token" content="{attr_token}">\n'
            "  <script>(function(){var t="
            f"{js_token};"
            "var of=window.fetch;if(!of)return;window.fetch=function(input,init){"
            "init=init||{};var m=(init.method||(input&&typeof input==='object'&&input.method)||'GET');"
            "m=String(m).toUpperCase();"
            "if(m==='POST'||m==='PUT'||m==='DELETE'){"
            "var h=new Headers((init&&init.headers)||(input&&typeof input==='object'&&input.headers)||{});"
            "if(!h.has('X-Agentic-Token'))h.set('X-Agentic-Token',t);init.headers=h;}"
            "return of.call(this,input,init);};})();</script>"
        )
        return html.replace("<head>", snippet, 1)

    def _send_error_envelope(
        self,
        status: int,
        error: str,
        *,
        message: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        body: Dict[str, Any] = {"error": error, "status": int(status)}
        if message:
            body["message"] = message
        if extra:
            body.update(extra)
        self._send_json(status, body)

    def _send_404(self) -> None:
        self._send_error_envelope(
            HTTPStatus.NOT_FOUND,
            "not_found",
            extra={"path": self.path},
        )

    def _send_usage_error(self, exc: UsageError) -> None:
        self._send_error_envelope(HTTPStatus.BAD_REQUEST, "bad_request", message=str(exc))

    def _send_404_or_405(self, method: str, path: str) -> None:
        allowed = _ROUTES.allowed_methods(path)
        if not allowed or method.upper() in allowed:
            self._send_404()
            return
        data = json.dumps(
            {
                "error": "method_not_allowed",
                "status": int(HTTPStatus.METHOD_NOT_ALLOWED),
                "allowed": allowed,
                "path": path,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Allow", ", ".join(allowed))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
