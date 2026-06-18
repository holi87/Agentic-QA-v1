"""ServerLifecycleMixin — extracted from routes/dashboard_server.py (issue #292)."""
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



class ServerLifecycleMixin:
    """Methods grouped by domain; merged into ``_Handler`` via MRO."""

    def _request_host_is_local(self) -> bool:
        """True when the `Host` header points at the loopback binding.

        Blocks DNS-rebinding attacks where a controlled DNS name resolves
        to 127.0.0.1 and the victim's browser POSTs to it carrying a
        `Host: attacker.com` header. We refuse such requests because the
        server is only meant to be reached via its loopback binding.
        Requests without a `Host` header (HTTP/1.0 clients, raw curl with
        `-H 'Host:'`) are accepted since the kernel binding already
        restricts reachability to the loopback interface.
        """
        host_hdr = self.headers.get("Host", "")
        if not host_hdr:
            return True
        if host_hdr.startswith("["):
            end = host_hdr.find("]")
            host = host_hdr[1:end] if end != -1 else host_hdr
        else:
            host = host_hdr.split(":", 1)[0]
        return host in _ALLOWED_LOCAL_HOSTS

    def _origin_is_local_or_absent(self) -> bool:
        """True when the request's `Origin` / `Referer` is loopback or absent.

        Blocks browser cross-origin POST (CSRF). CLI tools (curl, the test
        suite) do not set `Origin`, so an absent value passes — the
        loopback binding plus the Host check still keep the surface local.
        """
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        return (parsed.hostname or "") in _ALLOWED_LOCAL_HOSTS

    def _enforce_unsafe_request(self) -> bool:
        """Gate POSTs through Host + Origin checks.

        Returns True if the request may proceed. On rejection, sends a
        403 response and returns False — callers must short-circuit.
        """
        if not self._request_host_is_local():
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "forbidden_host",
                    "message": "dashboard rejects non-local Host header (DNS rebinding hardening)",
                },
            )
            return False
        if not self._origin_is_local_or_absent():
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "forbidden_origin",
                    "message": "cross-origin write rejected (CSRF guard)",
                },
            )
            return False
        if not self._request_token_is_valid():
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "forbidden_token",
                    "message": (
                        "unsafe method requires a valid X-Agentic-Token header "
                        "(issue #291 dashboard write-endpoint auth)"
                    ),
                },
            )
            return False
        return True

    def _request_token_is_valid(self) -> bool:
        """True when the request carries the server's unsafe-method token.

        Establishes caller identity for local unsafe writes (issue #291).
        The server-rendered dashboard embeds the token so its own fetches
        authenticate; a cross-origin page cannot read the response body to
        learn it, and a non-same-uid process cannot read the 0600 token
        file. Constant-time compare avoids leaking the token via timing.
        """
        expected = self.dashboard_token
        if not expected:
            # Defensive: `make_server` always provisions a token, so this is
            # only reachable when `_Handler` is instantiated directly (e.g.
            # a unit test) without binding a token. Fall back to the loopback
            # + Host + Origin guards already enforced above.
            return True
        presented = self.headers.get("X-Agentic-Token", "")
        return hmac.compare_digest(presented, expected)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._send_template("index.html")
            return
        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/tasks":
            self._send_template("tasks_list.html")
            return
        if path == "/tasks/new":
            self._send_template("tasks_new.html")
            return
        if path.startswith("/tasks/"):
            self._send_template("tasks_detail.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return
        if path.startswith("/files/"):
            self._serve_runtime_file(path[len("/files/"):])
            return
        if path.startswith("/task/"):
            self._send_template("task.html")
            return
        if path.startswith("/decision/"):
            self._send_template("decision.html")
            return
        if path == "/agents":
            self._send_template("agents.html")
            return
        if path == "/orchestration":
            self._send_template("orchestration.html")
            return
        if path == "/verifications":
            self._send_template("verifications.html")
            return
        if path == "/schedules":
            self._send_template("schedules.html")
            return
        if path == "/health":
            self._send_template("health.html")
            return
        if path == "/sessions":
            self._send_template("sessions.html")
            return
        if path == "/sessions/compare":
            self._send_template("sessions_compare.html")
            return
        if path.startswith("/sessions/"):
            self._send_template("sessions_detail.html")
            return
        if path == "/skills":
            self._send_template("skills.html")
            return
        if path == "/learnings":
            self._send_template("learnings.html")
            return
        if path == "/help":
            self._send_template("help.html")
            return
        if path == "/api/help":
            self._serve_help_doc()
            return
        if path.startswith("/docs/") and path.endswith(".md"):
            self._serve_doc_page(path[len("/docs/"):])
            return
        if path == "/api/status":
            self._serve_status()
            return
        if path == "/api/dashboard/overview":
            self._serve_dashboard_overview()
            return
        if path == "/api/dashboard/preflight":
            self._serve_dashboard_preflight()
            return
        if path == "/api/dashboard/charts":
            self._serve_dashboard_charts()
            return
        # Issue #314 (Wave 14) — unified metrics cockpit.
        if path == "/api/metrics":
            self._serve_metrics_json()
            return
        if path == "/metrics":
            self._serve_metrics_prometheus()
            return
        if path == "/metrics-cockpit":
            self._send_template("metrics_cockpit.html")
            return
        if path == "/api/config":
            self._serve_config()
            return
        if path == "/api/tasks":
            self._serve_work_items()
            return
        if path.startswith("/api/tasks/"):
            self._serve_work_item_path(path[len("/api/tasks/"):])
            return
        if path.startswith("/api/task/"):
            self._serve_task(path[len("/api/task/"):])
            return
        if path.startswith("/api/decision/"):
            self._serve_decision(path[len("/api/decision/"):])
            return
        if path == "/api/patches":
            self._serve_patches(None)
            return
        if path.startswith("/api/patches/"):
            self._serve_patches(path[len("/api/patches/"):])
            return
        if path == "/api/sut/git/status":
            self._serve_sut_git_status()
            return
        if path == "/api/sut/git/diff":
            self._serve_sut_git_diff(parsed.query)
            return
        if path == "/api/decisions":
            self._serve_decisions(parsed.query)
            return
        if path == "/api/agents":
            self._serve_agents()
            return
        if path == "/api/skills":
            self._serve_skills()
            return
        if path.startswith("/api/skills/"):
            self._serve_skill_detail(path[len("/api/skills/"):])
            return
        if path == "/api/suggestions":
            self._serve_suggestions()
            return
        if path == "/api/autonomy/status":
            self._serve_autonomy_status()
            return
        if path == "/api/autonomy/preflight":
            self._serve_autonomy_preflight()
            return
        if path == "/api/budget/status":
            self._serve_budget_status(parsed.query)
            return
        if path == "/api/providers/cooldowns":
            self._serve_provider_cooldowns(parsed.query)
            return
        if path == "/api/sessions":
            self._serve_sessions_list(parsed.query)
            return
        if path == "/api/sessions/compare":
            self._serve_sessions_compare(parsed.query)
            return
        if path.startswith("/api/sessions/"):
            self._serve_session_detail(path[len("/api/sessions/"):])
            return
        if path.startswith("/api/transcripts/"):
            self._serve_transcript(path[len("/api/transcripts/"):])
            return
        if path == "/api/inbox":
            self._serve_inbox_list()
            return
        if path == "/api/events":
            self._serve_events_sse()
            return
        if path == "/api/events/history":
            self._serve_events_history(parsed.query)
            return
        if path == "/api/schedules":
            self._serve_schedules()
            return
        if path == "/api/learnings":
            self._serve_learnings_list(parsed.query)
            return
        if path.startswith("/api/learnings/"):
            self._serve_learning_detail(path[len("/api/learnings/"):])
            return
        if path == "/api/health/repair":
            self._serve_health_repair()
            return
        self._send_404_or_405("GET", path)

    def do_POST(self) -> None:  # noqa: N802
        if not self._enforce_unsafe_request():
            return
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/tasks":
            self._create_work_item()
            return
        if path == "/api/tasks/prune":
            self._prune_orphan_tasks_action()
            return
        if path == "/api/config":
            self._save_config()
            return
        for suffix in (
            "/analyze",
            "/plan",
            "/implement-tests",
            "/review-gate",
            "/apply-patch",  # Issue #80
            "/run-tests",
            "/final-gate",
        ):
            if path.startswith("/api/tasks/") and path.endswith(suffix):
                wid = path[len("/api/tasks/"):-len(suffix)]
                self._invoke_action(wid, suffix.lstrip("/"))
                return
        if path.startswith("/api/tasks/") and "/candidates/" in path:
            self._candidate_decision_action(path[len("/api/tasks/"):])
            return
        if path.startswith("/api/tasks/") and path.endswith("/abandon-patch"):
            wid = path[len("/api/tasks/"):-len("/abandon-patch")]
            self._abandon_patch_action(wid)
            return
        # SUT lifecycle endpoints.
        if path == "/api/sut/start":
            self._sut_lifecycle_action("start")
            return
        if path == "/api/sut/healthcheck":
            self._sut_lifecycle_action("healthcheck")
            return
        if path == "/api/sut/stop":
            self._sut_lifecycle_action("stop")
            return
        if path == "/api/runtime/recover":
            self._runtime_recovery_action()
            return
        # git operations.
        if path == "/api/sut/git/init":
            self._sut_git_action("init")
            return
        if path == "/api/sut/git/remote":
            self._sut_git_action("remote")
            return
        if path == "/api/sut/git/publish":
            self._sut_git_action("publish")
            return
        if path == "/api/sut/git/fetch":
            self._sut_git_action("fetch")
            return
        if path == "/api/sut/git/pull":
            self._sut_git_action("pull")
            return
        if path == "/api/sut/git/ensure":
            self._sut_git_ensure()
            return
        # Issue #247 — operator override of an autonomous decision.
        if path.startswith("/api/decisions/") and path.endswith("/override"):
            decision_id = path[len("/api/decisions/"):-len("/override")]
            self._decision_override(decision_id)
            return
        # agents.
        if path.startswith("/api/agents/") and path.endswith("/test"):
            role = path[len("/api/agents/"):-len("/test")]
            self._agent_test_action(role)
            return
        if path.startswith("/api/agents/") and path.count("/") == 3:
            role = path[len("/api/agents/"):]
            self._agent_update_action(role)
            return
        # skill enable/disable.
        if path.endswith("/enable") and path.startswith("/api/skills/"):
            sid = path[len("/api/skills/"):-len("/enable")]
            self._skill_toggle_action(sid, enable=True)
            return
        if path.endswith("/disable") and path.startswith("/api/skills/"):
            sid = path[len("/api/skills/"):-len("/disable")]
            self._skill_toggle_action(sid, enable=False)
            return
        # suggestions.
        if path == "/api/suggestions/refresh":
            self._suggestions_refresh()
            return
        # SUT mode toggle (local compose | online URL).
        if path == "/api/sut/mode":
            self._sut_mode_action()
            return
        # Full autonomy session control.
        if path == "/api/autonomy/start":
            self._autonomy_start_action()
            return
        if path == "/api/autonomy/stop":
            self._autonomy_stop_action()
            return
        if path == "/api/autonomy/pause":
            self._autonomy_pause_resume_action("pause")
            return
        if path == "/api/autonomy/resume":
            self._autonomy_pause_resume_action("resume")
            return
        if path.startswith("/api/sessions/") and path.endswith("/bookmark"):
            sid = path[len("/api/sessions/"):-len("/bookmark")]
            self._session_bookmark_action(sid)
            return
        if path.startswith("/api/learnings/") and path.endswith("/forget"):
            lid = path[len("/api/learnings/"):-len("/forget")]
            self._learning_forget_action(lid)
            return
        # Inbox ingest pipeline (md/markdown/txt/docx/pdf -> task spec).
        if path == "/api/inbox/upload":
            self._inbox_upload_action()
            return
        if path == "/api/inbox/ingest":
            self._inbox_ingest_action()
            return
        if path == "/api/inbox/synthesize":
            self._inbox_synthesize_action()
            return
        if path == "/api/support-bundle":
            self._support_bundle_action()
            return
        # Issue #274 — apply runtime repairs from the /health page.
        if path == "/api/health/repair/apply":
            self._health_repair_apply_action()
            return
        # Issue #271 — schedule CRUD + run-now.
        if path == "/api/schedules":
            self._create_schedule_action()
            return
        if path.startswith("/api/schedules/") and path.endswith("/enable"):
            name = unquote(path[len("/api/schedules/"):-len("/enable")])
            self._schedule_toggle_action(name, enable=True)
            return
        if path.startswith("/api/schedules/") and path.endswith("/disable"):
            name = unquote(path[len("/api/schedules/"):-len("/disable")])
            self._schedule_toggle_action(name, enable=False)
            return
        if path.startswith("/api/schedules/") and path.endswith("/run-now"):
            name = unquote(path[len("/api/schedules/"):-len("/run-now")])
            self._schedule_run_now_action(name)
            return
        self._send_404_or_405("POST", path)

    def do_DELETE(self) -> None:  # noqa: N802
        """Issue #224 — delete a work item from the dashboard."""
        if not self._enforce_unsafe_request():
            return
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path.startswith("/api/tasks/"):
            wid = path[len("/api/tasks/"):]
            if wid and "/" not in wid:
                self._delete_task_action(wid)
                return
        if path.startswith("/api/schedules/"):
            name = path[len("/api/schedules/"):]
            if name and "/" not in name:
                self._delete_schedule_action(unquote(name))
                return
        self._send_404_or_405("DELETE", path)

    def do_PUT(self) -> None:  # noqa: N802
        """Issue #225 — save edited generated test files from the dashboard."""
        if not self._enforce_unsafe_request():
            return
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path.startswith("/api/tasks/") and "/generated-tests/" in path:
            suffix = path[len("/api/tasks/"):]
            wid, _, rel = suffix.partition("/generated-tests/")
            if wid and rel:
                self._save_generated_test_action(wid, rel)
                return
        self._send_404_or_405("PUT", path)

    def _read_optional_json_body(self) -> Dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or raw_len == "0":
            return {}
        try:
            size = int(raw_len)
        except ValueError as exc:
            raise UsageError("invalid Content-Length") from exc
        if size == 0:
            return {}
        if size < 0 or size > _MAX_JSON_BODY_BYTES:
            raise UsageError(f"JSON body must be <= {_MAX_JSON_BODY_BYTES} bytes")
        data = self.rfile.read(size)
        if not data:
            return {}
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UsageError("invalid JSON body") from exc
        if body is None or body == {}:
            return {}
        if not isinstance(body, dict):
            raise UsageError("JSON body must be an object")
        return body

    def _read_json_body(self) -> Dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            raise UsageError("missing JSON body")
        try:
            size = int(raw_len)
        except ValueError as exc:
            raise UsageError("invalid Content-Length") from exc
        if size < 1 or size > _MAX_JSON_BODY_BYTES:
            raise UsageError(f"JSON body must be between 1 and {_MAX_JSON_BODY_BYTES} bytes")
        data = self.rfile.read(size)
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UsageError("invalid JSON body") from exc
        if not isinstance(body, dict):
            raise UsageError("JSON body must be an object")
        return body
