"""StaticMixin — extracted from routes/dashboard_server.py (issue #292)."""
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



class StaticMixin:
    """Methods grouped by domain; merged into ``_Handler`` via MRO."""

    _DOC_PAGE_WHITELIST = {
        "troubleshooting.md",
        "operator-guide.md",
        "dashboard-help.md",
    }

    def _serve_runtime_file(self, suffix: str) -> None:
        """Serve read-only runtime artifacts (reports, evidence, manifests, gates).

        Whitelist of root dirs is intentional — anything outside is 404. Path
        traversal is rejected before we touch the filesystem.
        """
        repo_root = self.paths.repo_root.resolve()
        # Issue #291 — route the path through the shared hardening helper so
        # NUL bytes, absolute paths, and `..` escapes are rejected uniformly
        # before we touch the filesystem. `resolve_repo_path` raises on any
        # of those; anything that survives is guaranteed under repo_root.
        if not suffix:
            self._send_404()
            return
        try:
            target = resolve_repo_path(repo_root, suffix, label="files path")
        except UsageError:
            self._send_404()
            return
        # Whitelist of served root dirs — resolve every entry so the
        # containment check compares fully-resolved paths on both sides
        # (a symlinked `reports/` must not slip past `_is_under`).
        allowed_roots = [
            (repo_root / "reports").resolve(),
            (repo_root / "bugs").resolve(),
            (self.paths.runtime_root / "analysis").resolve(),
            (self.paths.runtime_root / "plans").resolve(),
            self.paths.task_specs_dir.resolve(),
            (self.paths.runtime_root / "runs").resolve(),
            self.paths.evidence_dir.resolve(),
            self.paths.patches_dir.resolve(),
            self.paths.subprocess_logs_dir.resolve(),
            (self.paths.runtime_root / "support-bundles").resolve(),
        ]
        if not any(_is_under(target, root) for root in allowed_roots):
            self._send_404()
            return
        if not target.exists() or not target.is_file():
            self._send_404()
            return
        ext = target.suffix.lower()
        content_type = {
            ".json": "application/json",
            ".md": "text/markdown",
            ".html": "text/html",
            ".txt": "text/plain",
            ".log": "text/plain",
            ".patch": "text/plain",
            ".diff": "text/plain",
            ".gz": "application/gzip",
        }.get(ext, "application/octet-stream")
        if _is_under(target, self.paths.subprocess_logs_dir.resolve()):
            raw = target.read_text(encoding="utf-8", errors="replace")
            data = redact_sensitive_text(raw).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", _content_type_header(content_type))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        else:
            size = target.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", _content_type_header(content_type))
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with target.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=64 * 1024)

    def _serve_static(self, suffix: str) -> None:
        if not suffix or ".." in suffix.split("/"):
            self._send_404()
            return
        target = (STATIC_DIR / suffix).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send_404()
            return
        if not target.exists() or not target.is_file():
            self._send_404()
            return
        ctype = STATIC_CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_doc_page(self, name: str) -> None:
        """Render a whitelisted `docs/<name>.md` to a full HTML page.

        The dashboard help guide deep-links to operator-facing docs (e.g.
        `/docs/troubleshooting.md`). The `/files/*` handler only serves
        runtime artifacts, so we expose a separate, narrower route that
        renders the markdown server-side through `help_md.render`."""
        if "/" in name or ".." in name or name not in self._DOC_PAGE_WHITELIST:
            self._send_404()
            return
        source = self.paths.repo_root / "docs" / name
        if not source.is_file():
            self._send_404()
            return
        from ...help_md import render

        try:
            markdown = source.read_text(encoding="utf-8")
        except OSError:
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, "doc read failed")
            return
        body = render(markdown)
        title = name.removesuffix(".md").replace("-", " ").title()
        page = (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\"><head>"
            "<meta charset=\"utf-8\">"
            f"<title>{html_escape(title)} — Quality Cat Agentic Web Testing</title>"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<link rel=\"stylesheet\" href=\"/static/dashboard.css\">"
            "</head><body>"
            "<header class=\"topbar\" role=\"banner\">"
            f"<h1>{html_escape(title)}</h1>"
            "<nav class=\"nav\" aria-label=\"Primary\">"
            "<a class=\"nav-link\" href=\"/\">Application</a>"
            "<a class=\"nav-link\" href=\"/tasks\">Tasks</a>"
            "<a class=\"nav-link\" href=\"/help\">Help</a>"
            "</nav></header>"
            "<main><section class=\"card help-doc\">"
            f"{body}"
            "</section></main>"
            "</body></html>"
        )
        self._send_text(HTTPStatus.OK, page, content_type="text/html")

    def _serve_help_doc(self) -> None:
        """Render docs/dashboard-help.md to HTML for the in-product help page.

        The renderer is intentionally minimal — see help_md.render docstring."""
        from ...help_md import render

        source = self.paths.repo_root / "docs" / "dashboard-help.md"
        if not source.is_file():
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "help_doc_missing", "path": str(source)},
            )
            return
        try:
            markdown = source.read_text(encoding="utf-8")
        except OSError as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "help_doc_read_failed", "message": str(exc)},
            )
            return
        body = render(markdown)
        self._send_json(
            HTTPStatus.OK,
            {"source": "docs/dashboard-help.md", "html": body},
        )
