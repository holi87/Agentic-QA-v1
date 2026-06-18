"""SkillsMixin — extracted from routes/dashboard_server.py (issue #292)."""
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



class SkillsMixin:
    """Methods grouped by domain; merged into ``_Handler`` via MRO."""

    def _serve_agents(self) -> None:
        import shutil

        from ...config import ConfigError, load_or_default

        try:
            cfg = load_or_default(self.paths.repo_root).raw
        except ConfigError as exc:
            self._send_json(HTTPStatus.OK, {"agents": [], "error": str(exc)})
            return
        models = cfg.get("models") or {}
        out = []
        for role in ("planner", "implementer", "reviewer", "triager"):
            m = models.get(role) or {}
            command = m.get("command") or []
            binary = command[0] if command else None
            found = bool(binary and shutil.which(binary))
            out.append(
                {
                    "role": role,
                    "provider": m.get("provider"),
                    "command": list(command),
                    "binary_found": found,
                    "auto_fire": bool(m.get("auto_fire", role in ("planner", "implementer"))),
                }
            )
        self._send_json(HTTPStatus.OK, {"agents": out, "full_mode": is_full_mode_active()})

    def _agent_test_action(self, role: str) -> None:
        enabled, _ = _dashboard_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        import shutil

        from ...config import load_or_default
        from ...runtime.subprocess import run_command

        try:
            cfg = load_or_default(self.paths.repo_root).raw
        except ConfigError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        models = cfg.get("models") or {}
        m = models.get(role) or {}
        command = m.get("command") or []
        if not command:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "no_command", "role": role})
            return
        if not shutil.which(command[0]):
            self._send_json(
                HTTPStatus.OK,
                {"ok": False, "reason": "binary_not_on_path", "command": list(command)},
            )
            return
        log = self.paths.subprocess_logs_dir / f"agent-test-{role}.log"
        try:
            res = run_command(
                [command[0], "--version"],
                cwd=self.paths.repo_root,
                log_path=log,
                timeout_seconds=10,
            )
        except ValueError as exc:
            self._send_json(
                HTTPStatus.OK,
                {"ok": False, "reason": "binary_not_allowed", "message": str(exc)},
            )
            return
        out_text = ""
        try:
            out_text = log.read_text(encoding="utf-8", errors="replace")[:500]
        except OSError:
            pass
        self._send_json(
            HTTPStatus.OK,
            {"ok": res.exit_code == 0, "exit_code": res.exit_code, "log_excerpt": out_text},
        )

    def _agent_update_action(self, role: str) -> None:
        enabled, _ = _dashboard_config_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        if role not in {"planner", "implementer", "reviewer", "triager"}:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_role"})
            return
        try:
            body = self._read_optional_json_body()
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        if not isinstance(body, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "json_body_required"})
            return
        command = body.get("command")
        provider = body.get("provider")
        auto_fire = body.get("auto_fire")
        if command is not None and not (
            isinstance(command, list) and command and all(isinstance(c, str) for c in command)
        ):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_command"})
            return
        from ...config import ConfigError, load_or_default, write_config

        try:
            cfg = load_or_default(self.paths.repo_root)
        except ConfigError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        raw = cfg.raw
        models = raw.setdefault("models", {})
        m = models.setdefault(role, {})
        if "role" not in m:
            m["role"] = {
                "planner": "opus",
                "implementer": "sonnet",
                "reviewer": "codex",
                "triager": "gemini",
            }[role]
        if command is not None:
            m["command"] = list(command)
        if provider is not None:
            m["provider"] = provider
        if auto_fire is not None:
            m["auto_fire"] = bool(auto_fire)
        try:
            write_config(Path(cfg.source), raw)
        except ConfigError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "role": role})

    def _serve_skills(self) -> None:
        from ...skills import discover_skills, load_skills_config

        cfg_path = self.paths.repo_root / "config" / "skills.yml"
        try:
            cfg = load_skills_config(cfg_path)
        except ValueError as exc:
            self._send_json(HTTPStatus.OK, {"skills": [], "error": str(exc)})
            return
        project_skills = discover_skills(self.paths.repo_root / "skills", source="project")
        per_role = (cfg.get("skills") or {}).get("per_role") or {}
        out = []
        for skill in project_skills:
            roles_enabled = [r for r, p in per_role.items() if skill.skill_id in (p.get("enabled") or [])]
            out.append(
                {
                    "id": skill.skill_id,
                    "name": skill.name,
                    "description": skill.description,
                    "source": skill.source,
                    "size_bytes": skill.size_bytes,
                    "checksum": skill.checksum,
                    "roles_enabled": roles_enabled,
                    "tags": list(skill.tags),
                }
            )
        self._send_json(HTTPStatus.OK, {"scope": (cfg.get("skills") or {}).get("scope", "project"), "skills": out})

    def _serve_skill_detail(self, skill_id: str) -> None:
        from ...skills import load_skill

        # Path resolution: skill_id `claude/QC-x` -> skills/claude/QC-x.md
        try:
            skill = load_skill(self.paths.repo_root / "skills", skill_id + ".md")
        except ValueError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "id": skill.skill_id,
                "name": skill.name,
                "description": skill.description,
                "body": skill.body,
                "checksum": skill.checksum,
            },
        )

    def _skill_toggle_action(self, skill_id: str, *, enable: bool) -> None:
        enabled, _ = _dashboard_config_write_settings(self.paths)
        if not enabled:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "dashboard_write_disabled"})
            return
        try:
            body = self._read_optional_json_body()
        except UsageError as exc:
            self._send_usage_error(exc)
            return
        role = (body or {}).get("role")
        if role not in {"planner", "implementer", "reviewer", "triager"}:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_role"})
            return
        import yaml as _yaml

        cfg_path = self.paths.repo_root / "config" / "skills.yml"
        from ...skills import load_skills_config

        cfg = load_skills_config(cfg_path)
        per_role = (cfg.setdefault("skills", {}).setdefault("per_role", {}).setdefault(role, {}))
        enabled_list = list(per_role.get("enabled") or [])
        disabled_list = list(per_role.get("disabled") or [])
        if enable:
            if skill_id not in enabled_list:
                enabled_list.append(skill_id)
            if skill_id in disabled_list:
                disabled_list.remove(skill_id)
        else:
            if skill_id in enabled_list:
                enabled_list.remove(skill_id)
            if skill_id not in disabled_list:
                disabled_list.append(skill_id)
        per_role["enabled"] = enabled_list
        per_role["disabled"] = disabled_list
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(_yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        self._send_json(HTTPStatus.OK, {"ok": True, "role": role, "skill_id": skill_id, "enabled": enable})
