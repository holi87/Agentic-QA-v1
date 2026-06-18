"""Local dashboard for Agentic OS.

Stdlib HTTP server bound to 127.0.0.1 by default. Serves a static dashboard,
JSON status endpoints, task intake endpoints, and an SSE event stream. Write
endpoints are disabled unless local config explicitly enables them. Each
request opens its own SQLite connection because the runtime DB connection is
owned by the orchestrator thread.
"""
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
from .response_builders import ResponseBuildersMixin
from .server_lifecycle import ServerLifecycleMixin
from .handlers import (
    AutonomyMixin,
    ConfigMixin,
    DecisionsMixin,
    EventsMixin,
    InboxMixin,
    MetricsMixin,
    SessionsMixin,
    SkillsMixin,
    StaticMixin,
    SuggestionsMixin,
    WorkItemsMixin,
)

from ._dashboard_state import *  # noqa: F401,F403  re-exported for backward compat


class _Handler(
    ResponseBuildersMixin,
    ServerLifecycleMixin,
    StaticMixin,
    ConfigMixin,
    WorkItemsMixin,
    AutonomyMixin,
    MetricsMixin,
    SessionsMixin,
    InboxMixin,
    SkillsMixin,
    SuggestionsMixin,
    EventsMixin,
    DecisionsMixin,
    BaseHTTPRequestHandler,
):
    """Composite request handler (issue #292).

    Behaviour lives in the mixin classes under ``routes/response_builders.py``,
    ``routes/server_lifecycle.py``, and ``routes/handlers/*.py``. This class
    is the composition root that wires every domain into one HTTP handler.
    """

    server_version = "AgenticOS/0.1"
    paths: RuntimePaths  # set by serve()
    dashboard_token: str = ""  # set by make_server() — issue #291 unsafe-method auth

class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: D401
        # Browsers close idle keep-alive sockets and SSE streams routinely;
        # the resulting ConnectionResetError / BrokenPipeError is benign noise.
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)

def make_server(
    paths: RuntimePaths, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> ThreadingHTTPServer:
    _retention_sweep_on_startup(paths)
    token = _load_or_create_dashboard_token(paths)
    handler_cls = type(
        "BoundHandler", (_Handler,), {"paths": paths, "dashboard_token": token}
    )
    server = _QuietThreadingHTTPServer((host, port), handler_cls)
    server.daemon_threads = True
    server._shutdown_requested = False  # type: ignore[attr-defined]
    return server

def serve(
    paths: RuntimePaths,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    on_ready: Optional[Callable[[Tuple[str, int]], None]] = None,
) -> ThreadingHTTPServer:
    """Start server in a background thread and return it.

    Caller is responsible for calling server.shutdown() / server.server_close().
    Pass on_ready callback to receive the bound address once it's listening.
    """
    server = make_server(paths, host=host, port=port)
    thread = threading.Thread(target=server.serve_forever, name="agentic-os-dashboard", daemon=True)
    thread.start()
    if on_ready is not None:
        on_ready(server.server_address)
    return server

def serve_blocking(
    paths: RuntimePaths,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> int:
    server = make_server(paths, host=host, port=port)
    # Issue #271 — fire cron-style schedules while the daemon serves.
    scheduler = None
    try:
        from ..config import get_active_config_override
        from ..scheduler import ScheduleRunner

        scheduler = ScheduleRunner(
            paths, config_override=get_active_config_override()
        )
        scheduler.start()
    except Exception:
        scheduler = None
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        if scheduler is not None:
            scheduler.stop()
        server._shutdown_requested = True  # type: ignore[attr-defined]
        server.server_close()
    return 0
