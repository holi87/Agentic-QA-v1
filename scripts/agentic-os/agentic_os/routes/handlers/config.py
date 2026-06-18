"""ConfigMixin — extracted from routes/dashboard_server.py (issue #292)."""
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



class ConfigMixin:
    """Methods grouped by domain; merged into ``_Handler`` via MRO."""

    def _serve_config(self) -> None:
        try:
            cfg = load_or_default(self.paths.repo_root)
        except ConfigError as exc:
            self._send_json(HTTPStatus.OK, {"source": None, "error": str(exc)})
            return
        from ...config import redact_secrets

        safe_raw = redact_secrets(cfg.raw)
        sut = safe_raw.get("sut", {}) or {}
        dashboard = safe_raw.get("dashboard", {}) or {}
        healthcheck = sut.get("healthcheck", {}) or {}
        try:
            source = str(Path(cfg.source).resolve().relative_to(self.paths.repo_root.resolve()))
        except (AttributeError, ValueError):
            source = str(getattr(cfg, "source", ""))
        payload = {
            "source": source,
            "sut": {
                "kind": sut.get("kind"),
                "mode": sut.get("mode") or "local",
                "web": sut.get("web") or {"enabled": True},
                "api": sut.get("api") or {"enabled": True},
                "root": sut.get("root"),
                "compose_file": sut.get("compose_file"),
                "compose_project_name": sut.get("compose_project_name"),
                "autostart": sut.get("autostart"),
                "test_runner": sut.get("test_runner"),
                "install_shim_allowed": sut.get("install_shim_allowed"),
                "base_url": sut.get("base_url"),
                "api_base_url": sut.get("api_base_url"),
                "ui_url": sut.get("ui_url"),
                "openapi": sut.get("openapi"),
                "docs": sut.get("docs"),
                "credentials": sut.get("credentials"),
                "tests_dir": sut.get("tests_dir"),
                "tests": sut.get("tests"),
                "healthcheck": {
                    "command": list(healthcheck.get("command") or []),
                    "timeout_seconds": healthcheck.get("timeout_seconds"),
                    "retries": healthcheck.get("retries"),
                },
            },
            "dashboard": {
                "host": dashboard.get("host"),
                "port": dashboard.get("port"),
                "enable_write_endpoints": (
                    bool(dashboard.get("enable_write_endpoints"))
                    or is_full_mode_active()
                    or _autonomy_writes_active()
                ),
                "full_mode": is_full_mode_active(),
                "autonomy_unlocks_writes": _autonomy_writes_active(),
            },
            "git": safe_raw.get("git") or {},
        }
        self._send_json(HTTPStatus.OK, payload)

    def _save_config(self) -> None:
        """POST /api/config — write new config when write endpoints are enabled."""
        try:
            cfg = load_or_default(self.paths.repo_root)
        except ConfigError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"current config invalid: {exc}"})
            return
        dashboard = cfg.raw.get("dashboard", {}) or {}
        if not dashboard.get("enable_write_endpoints") and not is_full_mode_active():
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "dashboard_write_disabled",
                    "message": _CONFIG_WRITE_DISABLED_MSG,
                },
            )
            return
        try:
            payload = self._read_optional_json_body()
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        if not isinstance(payload, dict) or not payload:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "POST /api/config requires a JSON config object"},
            )
            return
        # Refuse hostile dashboard host change without operator decision.
        new_dashboard = payload.get("dashboard")
        if isinstance(new_dashboard, dict):
            host = new_dashboard.get("host")
            if host is not None and host != "127.0.0.1":
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "dashboard.host must remain 127.0.0.1 (operator decision required to change)"},
                )
                return
        from ...config import DEFAULT_CONFIG_REL, ConfigError as _CfgErr
        from ...config import write_config

        # Canonical config lives at config/agentic-os.yml. cfg.source is set by
        # load_config() on the happy path, but if it ever comes back unset we
        # fall back to the canonical path rather than resurrecting the legacy
        # `.qualitycat/` directory (issue #53).
        target = (
            Path(cfg.source)
            if getattr(cfg, "source", None)
            else self.paths.repo_root / DEFAULT_CONFIG_REL
        )
        try:
            write_config(target, payload)
        except _CfgErr as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "source": str(target.relative_to(self.paths.repo_root.resolve()))},
        )

    def _sut_lifecycle_action(self, kind: str) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "dashboard_write_disabled"},
            )
            return
        from ...config import ConfigError, load_or_default
        from ...sut_lifecycle import run_sut_healthcheck, run_sut_start, run_sut_stop

        try:
            cfg = load_or_default(self.paths.repo_root).raw
        except ConfigError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "config_invalid", "message": str(exc)})
            return
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "db_missing"})
            return
        events = event_log_for_paths(conn, self.paths)
        try:
            sut = cfg.get("sut") or {}
            sut_mode = sut.get("mode") or "local"
            if kind in ("start", "stop") and sut_mode == "online":
                # Online SUT — no compose lifecycle; report no-op success.
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "exit_code": 0,
                        "failure_kind": None,
                        "log_path": None,
                        "detail": {"skipped": "sut.mode=online"},
                    },
                )
                return
            if kind == "start":
                res = run_sut_start(
                    self.paths,
                    events,
                    compose_file=sut.get("compose_file"),
                    compose_project_name=sut.get("compose_project_name"),
                )
            elif kind == "stop":
                res = run_sut_stop(
                    self.paths,
                    events,
                    compose_file=sut.get("compose_file"),
                    compose_project_name=sut.get("compose_project_name"),
                )
            else:  # healthcheck
                hc = sut.get("healthcheck") or {}
                res = run_sut_healthcheck(
                    self.paths,
                    events,
                    command=hc.get("command") or [],
                    timeout_seconds=int(hc.get("timeout_seconds") or 30),
                    retries=int(hc.get("retries") or 0),
                )
        finally:
            conn.close()
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": res.ok,
                "exit_code": res.exit_code,
                "failure_kind": res.failure_kind,
                "log_path": str(res.log_path.relative_to(self.paths.repo_root)) if res.log_path else None,
                "detail": res.detail,
            },
        )

    def _sut_git_action(self, kind: str) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        from ...config import ConfigError, load_or_default
        from ...sut_repo import (
            git_fetch,
            git_init,
            git_publish_main,
            git_pull_ff,
            git_set_remote,
        )

        try:
            cfg = load_or_default(self.paths.repo_root).raw
        except ConfigError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "config_invalid", "message": str(exc)})
            return
        sut_root = (cfg.get("sut") or {}).get("root") or "."
        try:
            body = self._read_optional_json_body()
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "db_missing"})
            return
        events = event_log_for_paths(conn, self.paths)
        try:
            if kind == "init":
                res = git_init(self.paths, events, sut_root=sut_root)
            elif kind == "remote":
                url = (body or {}).get("url")
                if not url:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing_fields", "message": "url required"})
                    return
                res = git_set_remote(self.paths, events, sut_root=sut_root, remote_url=str(url))
            elif kind == "publish":
                res = git_publish_main(self.paths, events, sut_root=sut_root)
            elif kind == "fetch":
                res = git_fetch(self.paths, events, sut_root=sut_root)
            else:  # pull
                res = git_pull_ff(self.paths, events, sut_root=sut_root)
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        finally:
            conn.close()
        # Issue #240 — when git is absent, surface a 200 with ok=true so the
        # dashboard renders the widget in the disabled state without dragging
        # the autonomy loop into an error path.
        detail = res.detail if isinstance(res.detail, dict) else {}
        if detail.get("skipped"):
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "skipped": True,
                    "reason": detail.get("reason", "git_not_installed"),
                    "exit_code": res.exit_code,
                },
            )
            return
        self._send_json(HTTPStatus.OK, {"ok": res.ok, "exit_code": res.exit_code, "detail": res.detail})

    def _sut_git_ensure(self) -> None:
        """Issue #241 — wraps `agentic_os.sut_repo.git_ensure` for the dashboard."""
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        from ...config import ConfigError, load_or_default
        from ...sut_repo import git_ensure

        try:
            cfg = load_or_default(self.paths.repo_root).raw
        except ConfigError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "config_invalid", "message": str(exc)})
            return
        sut_root = (cfg.get("sut") or {}).get("root") or "."
        git_cfg = cfg.get("git") or {}
        try:
            conn = _open_db(self.paths)
        except FileNotFoundError:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "db_missing"})
            return
        events = event_log_for_paths(conn, self.paths)
        try:
            report = git_ensure(
                self.paths,
                events,
                git_config=git_cfg,
                sut_root=sut_root,
            )
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        finally:
            conn.close()
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": report.ok,
                "summary": report.summary,
                "ops": list(report.ops),
            },
        )

    def _serve_sut_git_diff(self, query: str) -> None:
        """Issue #242 — GET /api/sut/git/diff?work_item=<id> unified diff."""
        from urllib.parse import parse_qs

        from ...config import ConfigError, load_or_default
        from ...sut_repo import git_work_item_diff

        params = parse_qs(query or "")
        work_item_id = (params.get("work_item") or [None])[0]
        if not work_item_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing_work_item"})
            return
        try:
            cfg = load_or_default(self.paths.repo_root).raw
        except ConfigError:
            self._send_json(HTTPStatus.OK, {"ok": False, "error": "config_invalid"})
            return
        sut_root = (cfg.get("sut") or {}).get("root") or "."
        git_cfg = cfg.get("git") or {}
        base = git_cfg.get("origin_branch") or "main"
        title = None
        try:
            conn = _open_db(self.paths)
            try:
                from ...work_items import get_work_item

                row = get_work_item(conn, work_item_id)
                if row is not None:
                    title = row.get("title")
            finally:
                conn.close()
        except sqlite3.Error:
            title = None
        try:
            result = git_work_item_diff(
                self.paths,
                sut_root=sut_root,
                work_item_id=work_item_id,
                title=title,
                base=base,
            )
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        self._send_json(HTTPStatus.OK, result)

    def _serve_sut_git_status(self) -> None:
        from ...config import ConfigError, load_or_default
        from ...sut_repo import git_status

        try:
            cfg = load_or_default(self.paths.repo_root).raw
        except ConfigError:
            self._send_json(HTTPStatus.OK, {"initialized": False})
            return
        sut_root = (cfg.get("sut") or {}).get("root") or "."
        try:
            status = git_status(self.paths, sut_root=sut_root)
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        self._send_json(HTTPStatus.OK, status)

    def _sut_mode_action(self) -> None:
        """POST /api/sut/mode — update sut.mode + sut.web + sut.api in config."""
        enabled, _ = _dashboard_config_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        try:
            payload = self._read_optional_json_body()
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON body required"})
            return
        mode = payload.get("mode")
        if mode not in ("local", "online"):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "mode must be 'local' or 'online'"},
            )
            return
        web = payload.get("web") or {}
        api = payload.get("api") or {}
        if not isinstance(web, dict) or not isinstance(api, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "web/api must be objects"})
            return
        from ...config import ConfigError as _CfgErr
        from ...config import load_or_default, write_config

        try:
            cfg = load_or_default(self.paths.repo_root)
        except _CfgErr as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        raw = dict(cfg.raw)
        sut = dict(raw.get("sut") or {})
        sut["mode"] = mode
        sut["web"] = {
            "enabled": bool(web.get("enabled", True)),
            "url": str(web.get("url") or "").strip() or None,
        }
        if sut["web"]["url"] is None:
            sut["web"].pop("url", None)
        sut["api"] = {
            "enabled": bool(api.get("enabled", True)),
            "url": str(api.get("url") or "").strip() or None,
        }
        if sut["api"]["url"] is None:
            sut["api"].pop("url", None)
        # When switching to online, compose_file may become null.
        if mode == "online":
            sut["compose_file"] = None
            sut["autostart"] = False
        raw["sut"] = sut
        target = Path(cfg.source) if getattr(cfg, "source", None) else self.paths.repo_root / "config" / "agentic-os.yml"
        try:
            write_config(target, raw)
        except _CfgErr as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "mode": mode, "web": sut["web"], "api": sut["api"]})
