"""InboxMixin — extracted from routes/dashboard_server.py (issue #292)."""
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



class InboxMixin:
    """Methods grouped by domain; merged into ``_Handler`` via MRO."""

    def _serve_inbox_list(self) -> None:
        from ...inbox import (
            INTAKE_DIRNAMES,
            INBOX_DIRNAME,
            SUPPORTED_EXTS,
            classify_intake_file,
            list_inbox_files,
        )

        files = []
        for entry in list_inbox_files(self.paths):
            try:
                size = entry.stat().st_size
            except OSError:
                size = None
            files.append({
                "path": str(entry.relative_to(self.paths.repo_root)),
                "name": entry.name,
                "ext": entry.suffix.lower(),
                "size": size,
                "extraction": classify_intake_file(entry),
            })
        self._send_json(
            HTTPStatus.OK,
            {
                "files": files,
                "canonical_dir": INBOX_DIRNAME,
                "intake_dirs": list(INTAKE_DIRNAMES),
                "supported_extensions": sorted(SUPPORTED_EXTS),
            },
        )

    def _inbox_upload_action(self) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled", "message": _WRITE_DISABLED_MSG},
            )
            return
        try:
            payload = self._read_optional_json_body()
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        from ...inbox import SUPPORTED_EXTS, inbox_dir

        if not isinstance(payload, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "bad_request", "message": "upload requires JSON object"},
            )
            return
        filename = str(payload.get("filename") or "").strip()
        content_b64 = str(payload.get("content_base64") or "")
        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "bad_request", "message": "filename must be a plain basename"},
            )
            return
        ext = Path(filename).suffix.lower()
        if ext not in SUPPORTED_EXTS:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "unsupported_extension",
                    "message": f"unsupported extension {ext!r}",
                    "supported_extensions": sorted(SUPPORTED_EXTS),
                },
            )
            return
        import base64

        try:
            raw = base64.b64decode(content_b64, validate=True)
        except (ValueError, TypeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "bad_request", "message": "content_base64 is not valid base64"},
            )
            return
        if not raw:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "bad_request", "message": "content is empty"},
            )
            return
        if len(raw) > 4 * 1024 * 1024:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "too_large", "message": "content exceeds 4 MiB"},
            )
            return
        base = inbox_dir(self.paths)
        base.mkdir(parents=True, exist_ok=True)
        dest = base / filename
        counter = 2
        while dest.exists():
            dest = base / f"{Path(filename).stem}-{counter}{Path(filename).suffix}"
            counter += 1
        dest.write_bytes(raw)
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "path": str(dest.relative_to(self.paths.repo_root)),
                "bytes": len(raw),
            },
        )

    def _prune_orphan_tasks_action(self) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled", "message": _WRITE_DISABLED_MSG},
            )
            return
        try:
            payload = self._read_optional_json_body()
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        ids: Optional[list[str]] = None
        if isinstance(payload, dict):
            raw_ids = payload.get("ids")
            if isinstance(raw_ids, list):
                ids = [str(i) for i in raw_ids if isinstance(i, str) and i.strip()]
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "db_missing"})
            return
        events = event_log_for_paths(conn, self.paths)
        try:
            pruned = prune_orphan_work_items(conn, self.paths, events, ids=ids)
        finally:
            conn.close()
        self._send_json(
            HTTPStatus.OK,
            {"pruned": pruned, "count": len(pruned)},
        )

    def _inbox_ingest_action(self) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled", "message": _WRITE_DISABLED_MSG},
            )
            return
        from ...inbox import ingest_inbox

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "db_missing"})
            return
        events = event_log_for_paths(conn, self.paths)
        try:
            cfg = load_or_default(self.paths.repo_root)
            default_sut_root = str(cfg.raw["sut"]["root"])
        except ConfigError:
            default_sut_root = "."
        try:
            results = ingest_inbox(
                conn, self.paths, events, default_sut_root=default_sut_root,
            )
        finally:
            conn.close()
        self._send_json(
            HTTPStatus.OK,
            {
                "results": results,
                "created": sum(1 for r in results if r.get("status") == "created"),
                "failed": sum(1 for r in results if r.get("status") == "failed"),
            },
        )

    def _inbox_synthesize_action(self) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled", "message": _WRITE_DISABLED_MSG},
            )
            return
        try:
            payload = self._read_optional_json_body()
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        title = None
        if isinstance(payload, dict) and payload.get("title") is not None:
            title = str(payload.get("title") or "").strip() or None
        from ...inbox import synthesize_inbox_task

        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "db_missing"})
            return
        events = event_log_for_paths(conn, self.paths)
        try:
            cfg = load_or_default(self.paths.repo_root)
            default_sut_root = str(cfg.raw["sut"]["root"])
        except ConfigError:
            default_sut_root = "."
        try:
            result = synthesize_inbox_task(
                conn,
                self.paths,
                events,
                title=title,
                default_sut_root=default_sut_root,
            )
        finally:
            conn.close()
        self._send_json(HTTPStatus.OK, result)

    def _support_bundle_action(self) -> None:
        """Build a redacted diagnostic tarball and return its location.

        Write-gated to match `inbox/upload` because the bundle reads
        config + events + bug notes. Operators consume it by downloading
        from `/runtime/<path>` afterwards (see `_serve_runtime_file`).
        """
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled", "message": _WRITE_DISABLED_MSG},
            )
            return
        # Drain the body (the dashboard POSTs `{}`) so the connection
        # stays clean. UsageError on a malformed body is non-fatal here —
        # the builder takes no inputs from the request.
        try:
            self._read_optional_json_body()
        except UsageError:
            pass
        from ...support_bundle import build_support_bundle

        try:
            result = build_support_bundle(self.paths.repo_root, self.paths)
        except Exception as exc:  # noqa: BLE001 — surface as 500, no stack-trace leak
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": "support_bundle_failed",
                    "message": f"{exc.__class__.__name__}: {exc}",
                },
            )
            return
        # `/files/<rel>` is the runtime static-file route; the support
        # bundles directory was whitelisted alongside reports/bugs/runs
        # so the operator can download the tarball straight from the
        # browser without an extra hop.
        result["download_url"] = "/files/" + result["path"]
        self._send_json(HTTPStatus.OK, result)
