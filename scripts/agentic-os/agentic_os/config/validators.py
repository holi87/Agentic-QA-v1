"""Config schema validators extracted from config.py (issue #292)."""
from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..errors import ConfigError, UsageError
from .types import _ValidationCtx



_REQUIRED_TOP = (
    "runtime",
    "sut",
    "models",
    "dashboard",
    "paths",
    "reports",
    "gates",
)
_OPTIONAL_TOP = (
    "budgets",
    "events",
    "autonomy",
    "git",
    "notifications",
    "prompt_context",
    "project",
)

_REQUIRED_RUNTIME = (
    "root",
    "timezone",
    "max_parallel_tasks",
    "heartbeat_seconds",
    "lease_ttl_seconds",
    "stale_lease_seconds",
    "shutdown_grace_seconds",
    "timeouts",
)

# Issue #361 — optional runtime knobs (absence keeps existing configs valid).
# `max_parallel_per_role`: per-role override of the global `max_parallel_tasks`
# agent cap (e.g. {planner: 1, implementer: 4}).
_OPTIONAL_RUNTIME = ("max_parallel_per_role",)

_REQUIRED_TIMEOUTS = (
    "default_seconds",
    "docker_seconds",
    "test_seconds",
    "model_seconds",
    "report_seconds",
)

# Compose-lifecycle keys: required for a local (Compose-managed) SUT, optional
# for an external SUT (`mode: online`) — the OS connects to an external SUT and
# never starts it, so it never builds Compose argv. See ADR-0001 / issue #356.
# NOTE: `healthcheck` and `test_runner` are NOT here — the OS still probes the
# external SUT and runs the generated tests in online mode (consumers in
# cmd_docs / workflows read them), so they stay required in both modes.
_LOCAL_SUT_KEYS = (
    "compose_file",
    "compose_project_name",
    "autostart",
    "install_shim_allowed",
)
# Required in every mode: the SUT root, its readiness probe, and the test runner.
_REQUIRED_SUT_BASE = ("root", "healthcheck", "test_runner")
# Backwards-compatible strict set: local mode keeps every key required.
_REQUIRED_SUT = _REQUIRED_SUT_BASE + _LOCAL_SUT_KEYS

_REQUIRED_HEALTHCHECK = ("command", "timeout_seconds", "retries")
_REQUIRED_MODEL = ("provider", "command", "role")
_OPTIONAL_MODEL = ("auto_fire", "fallback", "fallback_signals", "cooldown_seconds")
_REQUIRED_FALLBACK = ("provider", "command", "role")
_OPTIONAL_FALLBACK = ("fallback_signals", "cooldown_seconds")
_MODEL_PROVIDERS = {"claude", "codex", "antigravity", "script"}
_MODEL_ROLES = {"opus", "sonnet", "haiku", "codex", "gemini", "script"}
_REQUIRED_PATHS = ("reports", "bugs", "evidence", "prompts")
_REQUIRED_REPORTS = (
    "copy_reports_script",
    "extract_last_run_script",
    "build_summary_script",
    "require_reports_on_failure",
)
_REQUIRED_GATES = (
    "known_bugs_fail_exit",
    "assertion_changes_require_decision",
    "exact_spec_failure_opens_bug",
    "require_functional_area_tag",
    "require_lifecycle_tag",
    "infrastructure_exit_code",
)

def _check_keys(
    ctx: _ValidationCtx,
    prefix: str,
    obj: Any,
    required: Tuple[str, ...],
    *,
    optional: Tuple[str, ...] = (),
) -> None:
    if not isinstance(obj, dict):
        ctx.fail(prefix, "mapping", type(obj).__name__)
        return
    missing = [k for k in required if k not in obj]
    if missing:
        ctx.fail(prefix, f"keys present: {required}", f"missing: {missing}")
    allowed = set(required) | set(optional)
    extra = sorted(set(obj.keys()) - allowed)
    if extra:
        ctx.fail(prefix, f"only keys: {sorted(allowed)}", f"unexpected: {extra}")

_OPTIONAL_SUT = (
    "kind",
    "mode",  # "local" (compose) | "online" (URL only)
    "web",           # { enabled: bool, url?: str }
    "api",           # { enabled: bool, url?: str }
    "db",            # external SUT DB: { ref_type: env|file|none, value: str }
    "base_url",
    "api_base_url",
    "ui_url",
    "openapi",
    "docs",
    "credentials",
    "tests_dir",
    "tests",
)
_SUT_MODES = {"local", "online"}
_SUT_KINDS = {"web", "api", "web_api"}
_SOURCE_TYPES = {"file", "url"}
_CREDS_REF_TYPES = {"env", "file", "none"}
_API_RUNNERS = {"playwright-ts", "pytest-httpx"}
_UI_RUNNERS = {"playwright-ts"}

def _check_url(ctx: _ValidationCtx, path: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        ctx.fail(path, "non-empty URL string", value)
        return
    if not (value.startswith("http://") or value.startswith("https://")):
        ctx.fail(path, "URL scheme http or https", value)

def _check_safe_relpath(ctx: _ValidationCtx, path: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        ctx.fail(path, "non-empty relative path", value)
        return
    if value.startswith("/"):
        ctx.fail(path, "relative path (no leading /)", value)
        return
    parts = value.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        ctx.fail(path, "no path traversal (..)", value)

def _check_source_list(ctx: _ValidationCtx, path: str, value: Any) -> None:
    if not isinstance(value, list) or not value:
        ctx.fail(path, "non-empty list of {type, value} entries", value)
        return
    for i, entry in enumerate(value):
        entry_path = f"{path}[{i}]"
        if not isinstance(entry, dict):
            ctx.fail(entry_path, "mapping with type/value", entry)
            continue
        kind = entry.get("type")
        if kind not in _SOURCE_TYPES:
            ctx.fail(f"{entry_path}.type", f"one of {sorted(_SOURCE_TYPES)}", kind)
            continue
        val = entry.get("value")
        if kind == "url":
            _check_url(ctx, f"{entry_path}.value", val)
        elif kind == "file":
            _check_safe_relpath(ctx, f"{entry_path}.value", val)
        extra = sorted(set(entry.keys()) - {"type", "value"})
        if extra:
            ctx.fail(entry_path, "only keys: type, value", f"unexpected: {extra}")

def _validate_sut_v2(
    ctx: _ValidationCtx, sut: Dict[str, Any], *, external: bool = False
) -> None:
    """Validate optional v2 sut.* fields. Backwards compatible: all optional.

    When ``external`` (mode: online) the SUT must expose at least one enabled
    endpoint URL, and the optional ``db`` reference is validated.
    """
    if "kind" in sut:
        if sut["kind"] not in _SUT_KINDS:
            ctx.fail("sut.kind", f"one of {sorted(_SUT_KINDS)}", sut["kind"])
    if "mode" in sut:
        if sut["mode"] not in _SUT_MODES:
            ctx.fail("sut.mode", f"one of {sorted(_SUT_MODES)}", sut["mode"])
    for ep_key in ("web", "api"):
        if ep_key in sut:
            ep = sut[ep_key]
            _check_keys(ctx, f"sut.{ep_key}", ep, ("enabled",), optional=("url",))
            if isinstance(ep, dict):
                _check_bool(ctx, f"sut.{ep_key}.enabled", ep.get("enabled"))
                if ep.get("enabled") and sut.get("mode") == "online":
                    url = ep.get("url")
                    if not url:
                        ctx.fail(
                            f"sut.{ep_key}.url",
                            "URL required when mode=online and enabled=true",
                            url,
                        )
                    else:
                        _check_url(ctx, f"sut.{ep_key}.url", url)
                elif "url" in ep and ep.get("url"):
                    _check_url(ctx, f"sut.{ep_key}.url", ep["url"])
    if external:
        has_endpoint_url = any(
            isinstance(sut.get(ep_key), dict)
            and sut[ep_key].get("enabled")
            and sut[ep_key].get("url")
            for ep_key in ("web", "api")
        )
        if not has_endpoint_url:
            ctx.fail(
                "sut.web/sut.api",
                "at least one enabled endpoint with a url (mode: online)",
                "no enabled web/api endpoint url",
            )
    if "db" in sut:
        db = sut["db"]
        _check_keys(ctx, "sut.db", db, ("ref_type", "value"))
        if isinstance(db, dict):
            ref_type = db.get("ref_type")
            if ref_type not in _CREDS_REF_TYPES:
                ctx.fail(
                    "sut.db.ref_type",
                    f"one of {sorted(_CREDS_REF_TYPES)} (no inline secrets)",
                    ref_type,
                )
            _check_string(ctx, "sut.db.value", db.get("value"))
    for url_key in ("base_url", "api_base_url", "ui_url"):
        if url_key in sut:
            _check_url(ctx, f"sut.{url_key}", sut[url_key])
    if "openapi" in sut:
        openapi = sut["openapi"]
        _check_keys(ctx, "sut.openapi", openapi, ("sources",))
        if isinstance(openapi, dict):
            _check_source_list(ctx, "sut.openapi.sources", openapi.get("sources"))
    if "docs" in sut:
        docs = sut["docs"]
        _check_keys(ctx, "sut.docs", docs, ("sources",))
        if isinstance(docs, dict):
            _check_source_list(ctx, "sut.docs.sources", docs.get("sources"))
    if "credentials" in sut:
        creds = sut["credentials"]
        _check_keys(ctx, "sut.credentials", creds, ("ref_type", "value"))
        if isinstance(creds, dict):
            ref_type = creds.get("ref_type")
            if ref_type not in _CREDS_REF_TYPES:
                ctx.fail(
                    "sut.credentials.ref_type",
                    f"one of {sorted(_CREDS_REF_TYPES)}",
                    ref_type,
                )
            value = creds.get("value")
            if ref_type == "none":
                if value not in (None, ""):
                    ctx.fail(
                        "sut.credentials.value", "null or empty when ref_type=none", value
                    )
            elif ref_type == "env":
                if not isinstance(value, str) or not value:
                    ctx.fail(
                        "sut.credentials.value", "non-empty env var name", value
                    )
                elif not value.replace("_", "").isalnum() or not value[0].isalpha():
                    ctx.fail(
                        "sut.credentials.value",
                        "env var name (letters, digits, underscore)",
                        value,
                    )
            elif ref_type == "file":
                _check_safe_relpath(ctx, "sut.credentials.value", value)
    if "tests_dir" in sut:
        _check_safe_relpath(ctx, "sut.tests_dir", sut["tests_dir"])
    if "tests" in sut:
        tests = sut["tests"]
        _check_keys(ctx, "sut.tests", tests, (), optional=("api", "ui"))
        if isinstance(tests, dict):
            if "api" in tests:
                api = tests["api"]
                _check_keys(ctx, "sut.tests.api", api, ("runner",))
                if isinstance(api, dict) and api.get("runner") not in _API_RUNNERS:
                    ctx.fail(
                        "sut.tests.api.runner",
                        f"one of {sorted(_API_RUNNERS)}",
                        api.get("runner"),
                    )
            if "ui" in tests:
                ui = tests["ui"]
                _check_keys(ctx, "sut.tests.ui", ui, ("runner",))
                if isinstance(ui, dict) and ui.get("runner") not in _UI_RUNNERS:
                    ctx.fail(
                        "sut.tests.ui.runner",
                        f"one of {sorted(_UI_RUNNERS)}",
                        ui.get("runner"),
                    )

def _check_int(ctx: _ValidationCtx, path: str, value: Any, *, minimum: int | None = None, maximum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        ctx.fail(path, "integer", value)
        return
    if minimum is not None and value < minimum:
        ctx.fail(path, f"integer >= {minimum}", value)
    if maximum is not None and value > maximum:
        ctx.fail(path, f"integer <= {maximum}", value)

def _check_string(ctx: _ValidationCtx, path: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        ctx.fail(path, "non-empty string", value)

def _check_bool(ctx: _ValidationCtx, path: str, value: Any) -> None:
    if not isinstance(value, bool):
        ctx.fail(path, "boolean", value)

def _check_const(ctx: _ValidationCtx, path: str, value: Any, expected: Any) -> None:
    if value != expected:
        ctx.fail(path, f"const {expected!r}", value)

def _check_number(ctx: _ValidationCtx, path: str, value: Any, *, minimum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        ctx.fail(path, "number", value)
        return
    if minimum is not None and float(value) < minimum:
        ctx.fail(path, f"number >= {minimum}", value)

def _validate_budgets(ctx: _ValidationCtx, budgets: Any) -> None:
    _check_keys(
        ctx,
        "budgets",
        budgets,
        (),
        optional=("session", "per_role", "per_work_item", "fail_mode"),
    )
    if not isinstance(budgets, dict):
        return
    if "fail_mode" in budgets and budgets.get("fail_mode") not in {"abort", "warn"}:
        ctx.fail("budgets.fail_mode", "abort|warn", budgets.get("fail_mode"))
    session = budgets.get("session")
    if session is not None:
        _check_keys(ctx, "budgets.session", session, (), optional=("max_tokens", "max_usd"))
        if isinstance(session, dict):
            if "max_tokens" in session:
                _check_int(ctx, "budgets.session.max_tokens", session.get("max_tokens"), minimum=1)
            if "max_usd" in session:
                _check_number(ctx, "budgets.session.max_usd", session.get("max_usd"), minimum=0)
    per_role = budgets.get("per_role")
    if per_role is not None:
        if not isinstance(per_role, dict):
            ctx.fail("budgets.per_role", "mapping", per_role)
        else:
            for role, limits in per_role.items():
                if role not in {"planner", "implementer", "reviewer", "triager"}:
                    ctx.fail(f"budgets.per_role.{role}", "known model role", role)
                    continue
                _check_keys(ctx, f"budgets.per_role.{role}", limits, (), optional=("max_tokens",))
                if isinstance(limits, dict) and "max_tokens" in limits:
                    _check_int(ctx, f"budgets.per_role.{role}.max_tokens", limits.get("max_tokens"), minimum=1)
    per_work_item = budgets.get("per_work_item")
    if per_work_item is not None:
        _check_keys(ctx, "budgets.per_work_item", per_work_item, (), optional=("max_tokens",))
        if isinstance(per_work_item, dict) and "max_tokens" in per_work_item:
            _check_int(ctx, "budgets.per_work_item.max_tokens", per_work_item.get("max_tokens"), minimum=1)

def _validate_model_fallback(
    ctx: _ValidationCtx,
    role: str,
    fallback: Any,
    *,
    primary_provider: Optional[str] = None,
) -> None:
    if fallback is None:
        return
    if not isinstance(fallback, list):
        ctx.fail(f"models.{role}.fallback", "list of fallback entries", fallback)
        return
    seen_providers: list[str] = [primary_provider] if primary_provider else []
    for idx, entry in enumerate(fallback):
        path = f"models.{role}.fallback[{idx}]"
        if not isinstance(entry, dict):
            ctx.fail(path, "object with provider/command/role", entry)
            continue
        _check_keys(ctx, path, entry, _REQUIRED_FALLBACK, optional=_OPTIONAL_FALLBACK)
        provider = entry.get("provider")
        if provider not in _MODEL_PROVIDERS:
            ctx.fail(f"{path}.provider", f"one of {sorted(_MODEL_PROVIDERS)}", provider)
        cmd = entry.get("command")
        if not isinstance(cmd, list) or not cmd or any(not isinstance(c, str) for c in cmd):
            ctx.fail(f"{path}.command", "non-empty list of strings", cmd)
        if entry.get("role") not in _MODEL_ROLES:
            ctx.fail(f"{path}.role", f"one of {sorted(_MODEL_ROLES)}", entry.get("role"))
        if "fallback_signals" in entry:
            _check_signal_patterns(
                ctx, f"{path}.fallback_signals", entry.get("fallback_signals")
            )
        if "cooldown_seconds" in entry:
            _check_int(
                ctx, f"{path}.cooldown_seconds", entry.get("cooldown_seconds"), minimum=0
            )
        if isinstance(provider, str):
            if provider in seen_providers:
                ctx.fail(
                    f"{path}.provider",
                    "unique provider across primary + fallback chain",
                    provider,
                )
            else:
                seen_providers.append(provider)

def _check_signal_patterns(ctx: _ValidationCtx, path: str, value: Any) -> None:
    """Validate that each entry compiles as a regex."""
    if not isinstance(value, list) or any(not isinstance(p, str) for p in value):
        ctx.fail(path, "list of regex strings", value)
        return
    import re as _re

    for idx, pat in enumerate(value):
        try:
            _re.compile(pat)
        except _re.error as exc:
            ctx.fail(f"{path}[{idx}]", "valid Python regex", f"{pat!r} ({exc})")

def _validate_events(ctx: _ValidationCtx, events: Any) -> None:
    """Issue #245 — events.step_progress_throttle config gate."""
    _check_keys(ctx, "events", events, (), optional=("step_progress_throttle",))
    if not isinstance(events, dict):
        return
    if "step_progress_throttle" in events:
        _check_int(
            ctx,
            "events.step_progress_throttle",
            events.get("step_progress_throttle"),
            minimum=0,
        )

_OPTIONAL_AUTONOMY = (
    "coverage_floor",
    "coverage_architect",
    "triage_batch",
    "exploratory_baseline",
    # Issue #290 — opt-in bounded task synthesis on an empty queue.
    "task_synthesis",
)
_OPTIONAL_GIT = (
    "enabled",
    "auto_init",
    "origin",
    "origin_branch",
    "auto_fetch",
    "auto_publish",
)
_GIT_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

def _validate_project(ctx: _ValidationCtx, project: Any) -> None:
    """Issue #288 — optional project: block selecting the active project."""
    _check_keys(ctx, "project", project, (), optional=("active",))
    if not isinstance(project, dict):
        return
    if "active" in project:
        _check_string(ctx, "project.active", project.get("active"))

def _validate_prompt_context(ctx: _ValidationCtx, prompt_context: Any) -> None:
    """Issue #293 — optional prompt_context: block for injected agent context."""
    _check_keys(
        ctx,
        "prompt_context",
        prompt_context,
        (),
        optional=(
            "architecture_enabled",
            "architecture_budget_tokens",
            "learnings_enabled",
            "learnings_budget_tokens",
            "memory_enabled",
            "memory_budget_tokens",
            "memory_top_k",
        ),
    )
    if not isinstance(prompt_context, dict):
        return
    if "architecture_enabled" in prompt_context:
        _check_bool(
            ctx,
            "prompt_context.architecture_enabled",
            prompt_context.get("architecture_enabled"),
        )
    if "architecture_budget_tokens" in prompt_context:
        _check_int(
            ctx,
            "prompt_context.architecture_budget_tokens",
            prompt_context.get("architecture_budget_tokens"),
            minimum=0,
        )
    if "learnings_enabled" in prompt_context:
        _check_bool(
            ctx,
            "prompt_context.learnings_enabled",
            prompt_context.get("learnings_enabled"),
        )
    if "learnings_budget_tokens" in prompt_context:
        _check_int(
            ctx,
            "prompt_context.learnings_budget_tokens",
            prompt_context.get("learnings_budget_tokens"),
            minimum=0,
        )
    if "memory_enabled" in prompt_context:
        _check_bool(
            ctx,
            "prompt_context.memory_enabled",
            prompt_context.get("memory_enabled"),
        )
    if "memory_budget_tokens" in prompt_context:
        _check_int(
            ctx,
            "prompt_context.memory_budget_tokens",
            prompt_context.get("memory_budget_tokens"),
            minimum=0,
        )
    if "memory_top_k" in prompt_context:
        _check_int(
            ctx,
            "prompt_context.memory_top_k",
            prompt_context.get("memory_top_k"),
            minimum=1,
        )

def _validate_git(ctx: _ValidationCtx, git: Any) -> None:
    """Issue #241 — optional declarative git: block."""
    _check_keys(ctx, "git", git, (), optional=_OPTIONAL_GIT)
    if not isinstance(git, dict):
        return
    for key in ("enabled", "auto_init", "auto_fetch", "auto_publish"):
        if key in git:
            _check_bool(ctx, f"git.{key}", git.get(key))
    if "origin" in git and git.get("origin") is not None:
        origin = git.get("origin")
        if not isinstance(origin, str) or not origin:
            ctx.fail("git.origin", "non-empty string or null", origin)
        else:
            from ..sut_repo import validate_remote_url

            try:
                validate_remote_url(origin)
            except UsageError as exc:
                ctx.fail("git.origin", "valid remote URL", str(exc))
    if "origin_branch" in git:
        branch = git.get("origin_branch")
        if not isinstance(branch, str) or not _GIT_BRANCH_RE.match(branch or ""):
            ctx.fail("git.origin_branch", "branch name matching [A-Za-z0-9._/-]+", branch)

_NOTIFICATION_KINDS = (
    "blocked",
    "budget_exceeded",
    "session_completed",
    "provider_chain_exhausted",
    "failover",
)

def _validate_notifications(ctx: _ValidationCtx, notif: Any) -> None:
    """Issue #268 — optional notifications: block (webhook/desktop/sound)."""
    _check_keys(ctx, "notifications", notif, (), optional=("enabled", "dedup_window_seconds", "channels"))
    if not isinstance(notif, dict):
        return
    if "enabled" in notif:
        _check_bool(ctx, "notifications.enabled", notif.get("enabled"))
    if "dedup_window_seconds" in notif:
        _check_int(ctx, "notifications.dedup_window_seconds", notif.get("dedup_window_seconds"), minimum=1)
    channels = notif.get("channels")
    if channels is None:
        return
    if not isinstance(channels, dict):
        ctx.fail("notifications.channels", "mapping", channels)
        return
    _check_keys(ctx, "notifications.channels", channels, (), optional=("webhook", "desktop", "sound"))
    for name, cfg in channels.items():
        if name not in ("webhook", "desktop", "sound"):
            continue
        if not isinstance(cfg, dict):
            ctx.fail(f"notifications.channels.{name}", "mapping", cfg)
            continue
        if name == "webhook":
            url = cfg.get("url")
            if not isinstance(url, str) or not url:
                ctx.fail("notifications.channels.webhook.url", "non-empty string", url)
        if name in ("desktop", "sound") and "enabled" in cfg:
            _check_bool(ctx, f"notifications.channels.{name}.enabled", cfg.get("enabled"))
        events = cfg.get("events")
        if events is not None:
            if not isinstance(events, list):
                ctx.fail(f"notifications.channels.{name}.events", "list", events)
            else:
                for ev in events:
                    if ev not in _NOTIFICATION_KINDS:
                        ctx.fail(
                            f"notifications.channels.{name}.events",
                            f"one of {list(_NOTIFICATION_KINDS)}",
                            ev,
                        )

_AUTONOMY_INT_KEYS = (
    "session_retention_days",
    "exploratory_crawl_depth",
    "exploratory_cooldown_seconds",
    # Issue #290 — cap on items synthesized per idle cycle (minimum 1).
    "task_synthesis_max_per_cycle",
)
_AUTONOMY_ENUM_KEYS = ("transcript_capture", "queue_policy")
_TRANSCRIPT_CAPTURE_MODES = ("always", "on_block", "never")
_QUEUE_POLICY_MODES = ("fifo", "priority", "dependency", "budget_fair", "hybrid")

def _validate_autonomy(ctx: _ValidationCtx, autonomy: Any) -> None:
    """Phase 3 — autonomy.* feature flags (bools) + #269 ints + #270 enums."""
    _check_keys(
        ctx, "autonomy", autonomy, (),
        optional=_OPTIONAL_AUTONOMY + _AUTONOMY_INT_KEYS + _AUTONOMY_ENUM_KEYS,
    )
    if not isinstance(autonomy, dict):
        return
    for key in _OPTIONAL_AUTONOMY:
        if key in autonomy:
            _check_bool(ctx, f"autonomy.{key}", autonomy.get(key))
    for key in _AUTONOMY_INT_KEYS:
        if key in autonomy:
            _check_int(ctx, f"autonomy.{key}", autonomy.get(key), minimum=1)
    if "transcript_capture" in autonomy and autonomy.get("transcript_capture") not in _TRANSCRIPT_CAPTURE_MODES:
        ctx.fail(
            "autonomy.transcript_capture",
            f"one of {list(_TRANSCRIPT_CAPTURE_MODES)}",
            autonomy.get("transcript_capture"),
        )
    if "queue_policy" in autonomy and autonomy.get("queue_policy") not in _QUEUE_POLICY_MODES:
        ctx.fail(
            "autonomy.queue_policy",
            f"one of {list(_QUEUE_POLICY_MODES)}",
            autonomy.get("queue_policy"),
        )

def _validate(raw: Any) -> List[str]:
    ctx = _ValidationCtx()
    _check_keys(ctx, "root", raw, _REQUIRED_TOP, optional=_OPTIONAL_TOP)
    if ctx.errors:
        return ctx.errors

    runtime = raw["runtime"]
    _check_keys(ctx, "runtime", runtime, _REQUIRED_RUNTIME, optional=_OPTIONAL_RUNTIME)
    if isinstance(runtime, dict):
        _check_safe_relpath(ctx, "runtime.root", runtime.get("root"))
        _check_string(ctx, "runtime.timezone", runtime.get("timezone"))
        _check_int(ctx, "runtime.max_parallel_tasks", runtime.get("max_parallel_tasks"), minimum=1, maximum=16)
        per_role = runtime.get("max_parallel_per_role")
        if per_role is not None:
            if not isinstance(per_role, dict):
                ctx.fail("runtime.max_parallel_per_role", "mapping", type(per_role).__name__)
            else:
                for role_name, limit in per_role.items():
                    _check_int(
                        ctx,
                        f"runtime.max_parallel_per_role.{role_name}",
                        limit,
                        minimum=1,
                        maximum=16,
                    )
        _check_int(ctx, "runtime.heartbeat_seconds", runtime.get("heartbeat_seconds"), minimum=5)
        _check_int(ctx, "runtime.lease_ttl_seconds", runtime.get("lease_ttl_seconds"), minimum=10)
        _check_int(ctx, "runtime.stale_lease_seconds", runtime.get("stale_lease_seconds"), minimum=10)
        _check_int(ctx, "runtime.shutdown_grace_seconds", runtime.get("shutdown_grace_seconds"), minimum=1)
        timeouts = runtime.get("timeouts")
        _check_keys(ctx, "runtime.timeouts", timeouts, _REQUIRED_TIMEOUTS)
        if isinstance(timeouts, dict):
            for k in _REQUIRED_TIMEOUTS:
                _check_int(ctx, f"runtime.timeouts.{k}", timeouts.get(k), minimum=1)

    sut = raw["sut"]
    # External SUT (mode: online): the OS connects to it and never starts it,
    # so the Compose-only keys are optional (kept allowed for migration) and
    # `root` is the only mandatory key. Reachability = the web/api URL(s),
    # enforced in _validate_sut_v2. See ADR-0001 / issue #356.
    external_sut = isinstance(sut, dict) and sut.get("mode") == "online"
    if external_sut:
        _check_keys(
            ctx, "sut", sut, _REQUIRED_SUT_BASE,
            optional=_OPTIONAL_SUT + _LOCAL_SUT_KEYS,
        )
    else:
        _check_keys(ctx, "sut", sut, _REQUIRED_SUT, optional=_OPTIONAL_SUT)
    if isinstance(sut, dict):
        _validate_sut_v2(ctx, sut, external=external_sut)
        _check_string(ctx, "sut.root", sut.get("root"))
        # Compose-only keys: required (and thus present) in local mode, optional
        # in external mode — validate each only when supplied.
        if "compose_file" in sut:
            cf = sut.get("compose_file")
            if cf is not None and (not isinstance(cf, str) or not cf):
                ctx.fail("sut.compose_file", "string or null", cf)
        if "compose_project_name" in sut:
            _check_string(ctx, "sut.compose_project_name", sut.get("compose_project_name"))
        if "autostart" in sut:
            _check_bool(ctx, "sut.autostart", sut.get("autostart"))
        if "healthcheck" in sut:
            hc = sut.get("healthcheck")
            _check_keys(ctx, "sut.healthcheck", hc, _REQUIRED_HEALTHCHECK)
            if isinstance(hc, dict):
                cmd = hc.get("command")
                if not isinstance(cmd, list) or not cmd or any(not isinstance(c, str) for c in cmd):
                    ctx.fail("sut.healthcheck.command", "non-empty list of strings", cmd)
                _check_int(ctx, "sut.healthcheck.timeout_seconds", hc.get("timeout_seconds"), minimum=1)
                _check_int(ctx, "sut.healthcheck.retries", hc.get("retries"), minimum=0)
        if "test_runner" in sut:
            _check_string(ctx, "sut.test_runner", sut.get("test_runner"))
        if "install_shim_allowed" in sut:
            _check_bool(ctx, "sut.install_shim_allowed", sut.get("install_shim_allowed"))

    models = raw["models"]
    _check_keys(
        ctx,
        "models",
        models,
        ("planner", "implementer", "reviewer"),
        optional=("triager",),
    )
    if isinstance(models, dict):
        for role in ("planner", "implementer", "reviewer", "triager"):
            m = models.get(role)
            if m is None and role == "triager":
                continue  # triager optional
            _check_keys(ctx, f"models.{role}", m, _REQUIRED_MODEL, optional=_OPTIONAL_MODEL)
            if isinstance(m, dict):
                if m.get("provider") not in _MODEL_PROVIDERS:
                    ctx.fail(f"models.{role}.provider", f"one of {sorted(_MODEL_PROVIDERS)}", m.get("provider"))
                cmd = m.get("command")
                if not isinstance(cmd, list) or not cmd or any(not isinstance(c, str) for c in cmd):
                    ctx.fail(f"models.{role}.command", "non-empty list of strings", cmd)
                if m.get("role") not in _MODEL_ROLES:
                    ctx.fail(f"models.{role}.role", f"one of {sorted(_MODEL_ROLES)}", m.get("role"))
                if "auto_fire" in m and not isinstance(m["auto_fire"], bool):
                    ctx.fail(f"models.{role}.auto_fire", "boolean", m["auto_fire"])
                # Issue #235 — optional fallback chain. Each entry mirrors the
                # primary model shape; `fallback_signals` overrides per-entry
                # regex set, `cooldown_seconds` overrides per-entry cooldown.
                if "fallback" in m:
                    primary_provider = m.get("provider") if isinstance(m.get("provider"), str) else None
                    _validate_model_fallback(
                        ctx,
                        role,
                        m.get("fallback"),
                        primary_provider=primary_provider,
                    )
                if "fallback_signals" in m:
                    _check_signal_patterns(
                        ctx, f"models.{role}.fallback_signals", m.get("fallback_signals")
                    )
                if "cooldown_seconds" in m:
                    _check_int(
                        ctx,
                        f"models.{role}.cooldown_seconds",
                        m.get("cooldown_seconds"),
                        minimum=0,
                    )

    dashboard = raw["dashboard"]
    _check_keys(ctx, "dashboard", dashboard, ("host", "port", "enable_write_endpoints"))
    if isinstance(dashboard, dict):
        _check_const(ctx, "dashboard.host", dashboard.get("host"), "127.0.0.1")
        _check_int(ctx, "dashboard.port", dashboard.get("port"), minimum=1024, maximum=65535)
        _check_bool(ctx, "dashboard.enable_write_endpoints", dashboard.get("enable_write_endpoints"))

    paths = raw["paths"]
    _check_keys(ctx, "paths", paths, _REQUIRED_PATHS)
    if isinstance(paths, dict):
        for k in _REQUIRED_PATHS:
            _check_string(ctx, f"paths.{k}", paths.get(k))

    reports = raw["reports"]
    _check_keys(ctx, "reports", reports, _REQUIRED_REPORTS)
    if isinstance(reports, dict):
        for k in ("copy_reports_script", "extract_last_run_script", "build_summary_script"):
            _check_string(ctx, f"reports.{k}", reports.get(k))
        _check_const(ctx, "reports.require_reports_on_failure", reports.get("require_reports_on_failure"), True)

    gates = raw["gates"]
    _check_keys(ctx, "gates", gates, _REQUIRED_GATES)
    if isinstance(gates, dict):
        _check_const(ctx, "gates.known_bugs_fail_exit", gates.get("known_bugs_fail_exit"), True)
        _check_const(ctx, "gates.assertion_changes_require_decision", gates.get("assertion_changes_require_decision"), True)
        _check_const(ctx, "gates.exact_spec_failure_opens_bug", gates.get("exact_spec_failure_opens_bug"), True)
        _check_const(ctx, "gates.require_functional_area_tag", gates.get("require_functional_area_tag"), True)
        _check_const(ctx, "gates.require_lifecycle_tag", gates.get("require_lifecycle_tag"), True)
        _check_const(ctx, "gates.infrastructure_exit_code", gates.get("infrastructure_exit_code"), 2)

    if "budgets" in raw:
        _validate_budgets(ctx, raw.get("budgets"))

    if "events" in raw:
        _validate_events(ctx, raw.get("events"))

    if "autonomy" in raw:
        _validate_autonomy(ctx, raw.get("autonomy"))

    if "git" in raw:
        _validate_git(ctx, raw.get("git"))

    if "notifications" in raw:
        _validate_notifications(ctx, raw.get("notifications"))

    if "prompt_context" in raw:
        _validate_prompt_context(ctx, raw.get("prompt_context"))

    if "project" in raw:
        _validate_project(ctx, raw.get("project"))

    return ctx.errors
