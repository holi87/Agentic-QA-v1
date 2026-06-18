"""Module-level constants + helpers extracted from dashboard_server.py (issue #292).

Lives in its own module so the mixin classes under ``routes/handlers/`` can pull
in shared state without triggering a circular import back into
``routes/dashboard_server.py`` (which imports those mixins at module load time).
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


TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
STATIC_DIR = TEMPLATES_DIR / "static"
STATIC_CONTENT_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
NAV_LINKS: Tuple[Tuple[str, str], ...] = (
    ("Application", "/"),
    ("Tasks", "/tasks"),
    ("New task", "/tasks/new"),
    ("Agents", "/agents"),
    ("Skills", "/skills"),
    ("Orchestration", "/orchestration"),
    ("Verifications", "/verifications"),
    ("Sessions", "/sessions"),
    # Issue #314 (Wave 14) — unified metrics cockpit consumes /api/metrics.
    ("Metrics", "/metrics-cockpit"),
    ("Schedules", "/schedules"),
    ("Health", "/health"),
    ("Learnings", "/learnings"),
    ("Help", "/help"),
)
NAV_ACTIVE: Dict[str, str] = {
    "index.html": "/",
    "tasks_list.html": "/tasks",
    "tasks_new.html": "/tasks/new",
    "tasks_detail.html": "/tasks",
    # Issue #321 — detail views share the shell; map them to their parent section.
    "task.html": "/tasks",
    "decision.html": "/tasks",
    "agents.html": "/agents",
    "skills.html": "/skills",
    "orchestration.html": "/orchestration",
    "verifications.html": "/verifications",
    "sessions.html": "/sessions",
    "sessions_compare.html": "/sessions",
    "sessions_detail.html": "/sessions",
    "metrics_cockpit.html": "/metrics-cockpit",
    "schedules.html": "/schedules",
    "health.html": "/health",
    "learnings.html": "/learnings",
    "help.html": "/help",
}

NAV_SENTINEL = "<!-- DASHBOARD_NAV -->"

def render_nav(active_href: Optional[str]) -> str:
    """Build the canonical primary-nav block, marking ``active_href`` active.

    The active link uses the same `nav-link active` class the templates
    previously hand-wrote, so the rendered markup is byte-identical across
    pages once the `active` token is stripped (asserted in the UI contract
    tests).
    """
    lines = ['<nav class="nav" aria-label="Primary">']
    for label, href in NAV_LINKS:
        cls = "nav-link active" if href == active_href else "nav-link"
        lines.append(
            f'      <a class="{cls}" href="{html_escape(href, quote=True)}">'
            f"{html_escape(label)}</a>"
        )
    lines.append("    </nav>")
    return "\n".join(lines)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_SSE_POLL_SECONDS = 1.0
_SSE_KEEPALIVE_SECONDS = 15.0

_ROUTES = RouteDispatcher.from_specs(
    [
        ("GET", r"/", "_unused"),
        ("GET", r"/healthz", "_unused"),
        ("GET", r"/tasks", "_unused"),
        ("GET", r"/tasks/new", "_unused"),
        ("GET", r"/tasks/.+", "_unused"),
        ("GET", r"/static/.+", "_unused"),
        ("GET", r"/files/.+", "_unused"),
        ("GET", r"/task/.+", "_unused"),
        ("GET", r"/decision/.+", "_unused"),
        ("GET", r"/agents", "_unused"),
        ("GET", r"/skills", "_unused"),
        ("GET", r"/orchestration", "_unused"),
        ("GET", r"/verifications", "_unused"),
        ("GET", r"/schedules", "_unused"),
        ("GET", r"/health", "_unused"),
        ("GET", r"/learnings", "_unused"),
        ("GET", r"/help", "_unused"),
        ("GET", r"/docs/.+\.md", "_unused"),
        ("GET", r"/api/learnings", "_unused"),
        ("GET", r"/api/learnings/[0-9]+", "_unused"),
        ("POST", r"/api/learnings/[0-9]+/forget", "_unused"),
        ("GET", r"/api/(help|status|dashboard/overview|dashboard/preflight|dashboard/charts|config|tasks|patches|sut/git/status|sut/git/diff|decisions|agents|skills|suggestions|autonomy/status|autonomy/preflight|budget/status|providers/cooldowns|sessions|sessions/compare|inbox|events|events/history|metrics)", "_unused"),
        # Issue #314 (Wave 14) — Prometheus exposition path. Standard
        # exporters live at `/metrics` (no `/api/` prefix) so an external
        # Prometheus scrape config does not need a path rewrite. The
        # cockpit page is a regular HTML template that consumes
        # `/api/metrics`.
        ("GET", r"/metrics", "_unused"),
        ("GET", r"/metrics-cockpit", "_unused"),
        ("GET", r"/api/(tasks|task|decision|patches|skills|sessions|transcripts)/.+", "_unused"),
        ("GET", r"/api/schedules", "_unused"),
        ("GET", r"/api/health/repair", "_unused"),
        ("POST", r"/api/health/repair/apply", "_unused"),
        ("POST", r"/api/schedules", "_unused"),
        ("POST", r"/api/schedules/[^/]+/(enable|disable|run-now)", "_unused"),
        ("DELETE", r"/api/schedules/[^/]+", "_unused"),
        ("POST", r"/api/tasks", "_unused"),
        ("POST", r"/api/tasks/prune", "_unused"),
        ("POST", r"/api/config", "_unused"),
        ("POST", r"/api/tasks/.+/(analyze|plan|implement-tests|review-gate|apply-patch|run-tests|final-gate)", "_unused"),
        ("POST", r"/api/tasks/.+/candidates/.+", "_unused"),
        ("POST", r"/api/tasks/.+/abandon-patch", "_unused"),
        ("POST", r"/api/sut/(start|healthcheck|stop|mode)", "_unused"),
        ("POST", r"/api/runtime/recover", "_unused"),
        ("POST", r"/api/sut/git/(init|remote|publish|fetch|pull|ensure)", "_unused"),
        ("POST", r"/api/agents/.+", "_unused"),
        ("POST", r"/api/skills/.+/(enable|disable)", "_unused"),
        ("POST", r"/api/suggestions/refresh", "_unused"),
        ("POST", r"/api/autonomy/(start|stop|pause|resume)", "_unused"),
        ("POST", r"/api/sessions/[^/]+/bookmark", "_unused"),
        ("POST", r"/api/inbox/(upload|ingest|synthesize)", "_unused"),
        ("POST", r"/api/support-bundle", "_unused"),
        ("POST", r"/api/decisions/[^/]+/override", "_unused"),
        ("DELETE", r"/api/tasks/[^/]+", "_unused"),
        ("PUT", r"/api/tasks/.+/generated-tests/.+", "_unused"),
    ]
)
_ACTION_ORDER = (
    "analyze",
    "plan",
    "implement-tests",
    "review-gate",
    "apply-patch",
    "run-tests",
    "final-gate",
)

def _compute_action_gating(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    work_item_id: str,
) -> Dict[str, Dict[str, Any]]:
    """Return ``{action: {"enabled": bool, "reason": str}}`` for one task.

    The predicates use artifact kinds + plan candidate decisions (the
    durable record of what the workflow actually produced) rather than
    the work-item status enum, because status is downstream — an
    operator who imported analysis artifacts manually still deserves
    the `plan` button. See issue #194.
    """
    artifacts = list_work_item_artifacts(conn, work_item_id)
    kinds = {row["kind"] for row in artifacts}
    patch_states: set[str] = set()
    try:
        from ..gates import describe_blocking_patches

        patch_states = {
            str(row.get("state"))
            for row in describe_blocking_patches(paths, conn=conn, work_item_id=work_item_id)
        }
    except Exception:  # intentionally broad: blocking-patch lookup is advisory — degrade to "no blockers" rather than fail the request
        patch_states = set()
    unresolved_patch = bool(
        patch_states.intersection({"waiting", "approved_pending_apply"})
    )
    approved_pending_apply = "approved_pending_apply" in patch_states

    has_approved_candidate = False
    candidates_reason = ""
    if "test_plan" in kinds:
        try:
            payload = read_plan_candidates(paths, work_item_id=work_item_id)
        except UsageError as exc:
            candidates_reason = str(exc)
        else:
            for item in payload.get("items") or []:
                if item.get("decision") == "generate_now":
                    has_approved_candidate = True
                    break

    def _entry(enabled: bool, reason: str) -> Dict[str, Any]:
        return {"enabled": enabled, "reason": reason if not enabled else ""}

    gating: Dict[str, Dict[str, Any]] = {}
    # analyze is idempotent — operators re-analyze after editing the
    # spec or refreshing the OpenAPI doc, so it stays always-on.
    gating["analyze"] = _entry(True, "")
    gating["plan"] = _entry(
        "analysis" in kinds,
        "Run Analyze SUT first — no analysis artifacts on this task yet.",
    )
    if unresolved_patch:
        impl_reason = "Resolve the existing generated patch before generating another one."
    elif "test_plan" not in kinds:
        impl_reason = "Create a test plan first — no test_plan artifact yet."
    elif not has_approved_candidate:
        impl_reason = (
            "Approve at least one candidate (set decision=generate_now) "
            "in the plan before generating tests."
            + (f" Plan read error: {candidates_reason}" if candidates_reason else "")
        )
    else:
        impl_reason = ""
    gating["implement-tests"] = _entry(
        "test_plan" in kinds and has_approved_candidate and not unresolved_patch,
        impl_reason,
    )
    gating["review-gate"] = _entry(
        "patch" in kinds and not approved_pending_apply,
        (
            "Apply the approved patch first — review gate already passed."
            if approved_pending_apply
            else "Generate tests first — no patch artifact to review."
        ),
    )
    gating["apply-patch"] = _entry(
        approved_pending_apply,
        (
            "Run Review gate first — no approved patch is waiting to apply."
            if "patch" in kinds
            else "Generate tests first — no patch artifact to apply."
        ),
    )
    gating["run-tests"] = _entry(
        "apply" in kinds,
        "Apply the approved patch first — no apply artifact on this task.",
    )
    gating["final-gate"] = _entry(
        "run" in kinds,
        "Run tests first — no run manifest to evaluate.",
    )
    return gating

def _open_db(paths: RuntimePaths) -> sqlite3.Connection:
    if not paths.db.exists():
        raise FileNotFoundError(f"db missing: {paths.db}")
    return connect(paths.db)

def build_status(
    conn: sqlite3.Connection,
    paths: Optional[RuntimePaths] = None,
) -> Dict[str, Any]:
    tasks = fetch_task_summary(conn)
    # Issue #191 — the dashboard Runtime card needs the operator work-item
    # queue counters, not the internal scheduler `tasks` aggregate. We keep
    # the legacy `tasks` key for backwards compatibility (CLI/tests) and
    # add a `work_items` block that the JS will prefer for the queue card.
    work_items = work_item_summary(conn)
    bugs = fetch_bug_summary(conn)
    phases = fetch_phase_rows(conn)
    leases = fetch_active_leases(conn)
    last_run = fetch_last_run(conn, paths)
    blockers = list_open_blockers(conn)
    integrity = integrity_check(conn)

    runtime_state = "ready"
    if blockers:
        runtime_state = "degraded"
    if integrity != "ok":
        runtime_state = "blocked"

    return {
        "runtime": runtime_state,
        "db": "ok" if integrity == "ok" else "corrupt",
        "current_phase": CURRENT_PHASE_ID,
        "leases": leases,
        "phases": phases,
        "tasks": tasks,
        "work_items": work_items,
        "bugs": bugs,
        "last_run": last_run,
        "blockers_open": blockers,
    }

def generated_tests_for_work_item(paths: RuntimePaths, work_item_id: str) -> List[Dict[str, Any]]:
    """Return generated executable test files recorded by patch manifests."""
    out: List[Dict[str, Any]] = []
    root = paths.patches_dir / work_item_id
    if not root.exists():
        return out
    seen: set[str] = set()
    for manifest_path in sorted(root.glob("*/manifest.json")):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        files = payload.get("files")
        if not isinstance(files, list):
            continue
        try:
            manifest_rel = str(manifest_path.resolve().relative_to(paths.repo_root.resolve()))
        except ValueError:
            manifest_rel = str(manifest_path)
        for entry in files:
            if not isinstance(entry, dict):
                continue
            rel_path = str(entry.get("relative_path") or "").strip()
            if not rel_path or rel_path in seen:
                continue
            seen.add(rel_path)
            out.append(
                {
                    "relative_path": rel_path,
                    "candidate_id": entry.get("candidate_id"),
                    "runner": entry.get("runner"),
                    "manifest_path": manifest_rel,
                }
            )
    return out

def fetch_coverage_state(
    conn: sqlite3.Connection, work_item_id: str
) -> Optional[Dict[str, Any]]:
    """Derive the idempotent-no-op banner state for a work item.

    Issue #330 — when ``implement-tests`` is a no-op (every requested surface
    is already in the coverage ledger) the work item status is intentionally
    left untouched (PR #327), which leaves the operator without a visible
    signal that the task is *covered and has no further work*. Derive the
    signal here from the event log: the latest
    ``work_item.implement_idempotent_noop`` event wins, unless a newer
    ``work_item.patch_generated`` event for the same work item supersedes it
    (a fresh patch landed). Returns ``None`` when no signal applies so the
    dashboard can hide the banner.
    """
    # The LIKE pattern depends on `EventLog._insert` calling
    # ``json.dumps(payload, sort_keys=True)`` with default separators
    # (`: ` between key and value). If that ever changes to compact
    # separators (`,`/`:`), this match will silently miss rows and the
    # banner will stop appearing — keep both writers in sync.
    rows = conn.execute(
        """
        SELECT kind, ts, payload FROM events
         WHERE kind IN ('work_item.implement_idempotent_noop',
                        'work_item.patch_generated')
           AND payload LIKE ?
         ORDER BY ts DESC
         LIMIT 1;
        """,
        (f'%"work_item_id": "{work_item_id}"%',),
    ).fetchall()
    if not rows:
        return None
    row = rows[0]
    if row["kind"] != "work_item.implement_idempotent_noop":
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return None
    if payload.get("work_item_id") != work_item_id:
        return None
    skipped = payload.get("skipped_surfaces") or []
    return {
        "state": "covered",
        "ts": row["ts"],
        "skipped_surfaces": list(skipped) if isinstance(skipped, list) else [],
    }

def fetch_task_detail(conn: sqlite3.Connection, task_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT id, phase_id, parent_id, kind, status, payload, lease_owner, lease_expires,
               created_at, started_at, finished_at, exit_code, error_class, retry_of, updated_at
          FROM tasks WHERE id=?;
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    task = dict(row)
    try:
        task["payload"] = json.loads(task["payload"])
    except (TypeError, json.JSONDecodeError):
        pass
    runs = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, exit_code, duration_ms, log_path, evidence_path, manifest_path,
                   started_at, finished_at, failure_kind, unmapped_exit
              FROM runs WHERE task_id=? ORDER BY started_at DESC;
            """,
            (task_id,),
        ).fetchall()
    ]
    return {"task": task, "runs": runs}

def fetch_blocker_detail(conn: sqlite3.Connection, blocker_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT id, phase_id, severity, source, description, status, opened_at, closed_at
          FROM blockers WHERE id=?;
        """,
        (blocker_id,),
    ).fetchone()
    if row is None:
        return None
    return {"blocker": dict(row)}

def _workflow_payload(
    result: WorkflowResult, *, extra: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Map a workflow result to the dashboard JSON contract.

    Surfaces exit_code (0/1/2/130), failure_kind, and report path so the UI
    can render the green/yellow/red badge without re-reading the manifest.
    """
    payload: Dict[str, Any] = {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "failure_kind": result.failure_kind,
        "task_id": result.task_id,
        "run_id": result.run_id,
        "manifest_path": result.manifest_path,
        "reports_path": result.reports_path,
        "bugs_opened": result.bugs_opened,
    }
    if extra:
        payload.update(extra)
    return payload

def _is_under(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False

def _content_type_header(content_type: str) -> str:
    if content_type.startswith("text/") or content_type == "application/json":
        return f"{content_type}; charset=utf-8"
    return content_type

def _parse_kind_filter(raw_path: str):
    """Translate `?kind=foo,bar.*` query into a predicate over event.kind."""
    params = parse_qs(urlsplit(raw_path).query)
    raw = params.get("kind")
    if not raw:
        return None
    patterns: list[tuple[str, bool]] = []  # (token_without_star, is_prefix)
    for value in raw:
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            if token.endswith("*"):
                patterns.append((token[:-1], True))
            else:
                patterns.append((token, False))
    if not patterns:
        return None

    def matcher(kind: str) -> bool:
        for prefix, is_prefix in patterns:
            if is_prefix:
                if kind.startswith(prefix):
                    return True
            elif kind == prefix:
                return True
        return False

    return matcher

def _parse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw
_FULL_MODE_OVERRIDE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "agentic_os_full_mode_override",
    default=False,
)
_FULL_MODE_OVERRIDE_EVENT = threading.Event()

_WRITE_DISABLED_MSG = (
    "writes disabled — set dashboard.enable_write_endpoints=true, "
    "restart with `serve --full`, or start a full autonomy session"
)
_CONFIG_WRITE_DISABLED_MSG = (
    "config writes disabled — set dashboard.enable_write_endpoints=true "
    "or restart with `serve --full` (autonomy does not unlock config writes)"
)

def set_full_mode_override(enabled: bool) -> None:
    """Enable serve-full session override (forces enable_write_endpoints=true)."""
    _FULL_MODE_OVERRIDE.set(bool(enabled))
    if enabled:
        _FULL_MODE_OVERRIDE_EVENT.set()
    else:
        _FULL_MODE_OVERRIDE_EVENT.clear()

def is_full_mode_active() -> bool:
    return _FULL_MODE_OVERRIDE.get() or _FULL_MODE_OVERRIDE_EVENT.is_set()

def _autonomy_writes_active() -> bool:
    """True while a full-autonomy session is running.

    Autonomy is operator-acknowledged at start (via the dashboard confirm
    dialog), so we treat it as an implicit write-endpoint unlock for task
    creation / patching / decision endpoints. Config writes stay gated by
    enable_write_endpoints + serve --full to avoid silent mid-session
    configuration changes.

    Issue #292 split: this used to live in ``dashboard_server.py``; tests
    monkey-patch ``agentic_os.server._autonomy_writes_active`` (an alias
    of ``routes.dashboard_server``) to force-unlock writes. We honour that
    override by re-fetching the alias-module attribute on every call.
    """
    # Honour test-time monkey-patches on the dashboard_server module.
    from . import dashboard_server as _ds  # local import — avoids circular load

    impl = _ds.__dict__.get("_autonomy_writes_active")
    if impl is not None and impl is not _autonomy_writes_active:
        return impl()
    try:
        from ..autonomy import is_session_active

        return is_session_active()
    except Exception:  # intentionally broad: write-endpoint unlock gate must fail closed (no unlock) on any error
        return False

def _dashboard_write_settings(paths: RuntimePaths) -> tuple[bool, str]:
    autonomy_on = _autonomy_writes_active()
    try:
        cfg = load_or_default(paths.repo_root)
    except ConfigError:
        return is_full_mode_active() or autonomy_on, "."
    enabled = (
        bool(cfg.raw["dashboard"]["enable_write_endpoints"])
        or is_full_mode_active()
        or autonomy_on
    )
    return enabled, str(cfg.raw["sut"]["root"])

def _dashboard_config_write_settings(paths: RuntimePaths) -> tuple[bool, str]:
    """Stricter gate for endpoints that persist configuration on disk.

    Autonomy is intentionally NOT a sufficient unlock here — a running
    session must not be able to rewrite `agentic-os.yml`, `config/skills.yml`,
    or agent definitions while it is running. Operators have to opt in
    via `dashboard.enable_write_endpoints` or `serve --full`.
    """
    try:
        cfg = load_or_default(paths.repo_root)
    except ConfigError:
        return is_full_mode_active(), "."
    enabled = (
        bool(cfg.raw["dashboard"]["enable_write_endpoints"])
        or is_full_mode_active()
    )
    return enabled, str(cfg.raw["sut"]["root"])

def _retention_sweep_on_startup(paths: RuntimePaths) -> None:
    """Issue #269 — archive NDJSON older than autonomy.session_retention_days.

    Best-effort: a config/IO failure must not prevent the dashboard from
    starting. DB session rows are kept for the index.
    """
    try:
        from ..config import load_or_default
        from ..sessions import sweep_retention

        cfg = load_or_default(paths.repo_root)
        autonomy_cfg = (cfg.raw.get("autonomy") or {}) if isinstance(cfg.raw, dict) else {}
        days = autonomy_cfg.get("session_retention_days", 30)
        sweep_retention(paths, retention_days=int(days) if isinstance(days, int) else 30)
    except Exception:
        pass

def _load_or_create_dashboard_token(paths: RuntimePaths) -> str:
    """Resolve the per-server unsafe-method auth token (issue #291).

    Precedence:
      1. `AGENTIC_DASHBOARD_TOKEN` env — lets the CLI/operator pin a value.
      2. An existing `<runtime_root>/.dashboard_token` file (0600).
      3. A fresh `secrets.token_urlsafe(32)`, persisted with 0600 perms so
         same-uid CLI clients can read it while other users cannot.

    The token establishes caller identity for local unsafe writes. It is
    embedded in served HTML (safe against cross-origin CSRF reads) and
    written to a 0600 file (read by the same-uid operator/CLI). It does NOT
    defend against a malicious same-uid process that can read the file —
    that boundary is the OS user account, documented in
    docs/security_trust_boundary.md.
    """
    env_token = os.environ.get("AGENTIC_DASHBOARD_TOKEN")
    if env_token:
        return env_token
    token_file = paths.runtime_root / ".dashboard_token"
    try:
        existing = token_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic create at 0600 (O_EXCL) — no umask-dependent window where the
        # file briefly exists world-readable before a follow-up chmod. umask can
        # only clear bits, so the result is always a subset of 0600.
        fd = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
    except FileExistsError:
        # A concurrent server boot won the create race; adopt its token so all
        # processes share one valid credential.
        try:
            existing = token_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
    except OSError:
        # A read-only runtime dir must not crash the server; the in-memory
        # token still gates this process, it just is not persisted.
        pass
    return token


__all__ = [
    "TEMPLATES_DIR",
    "STATIC_DIR",
    "STATIC_CONTENT_TYPES",
    "NAV_LINKS",
    "NAV_ACTIVE",
    "NAV_SENTINEL",
    "render_nav",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "_ALLOWED_LOCAL_HOSTS",
    "_SSE_POLL_SECONDS",
    "_SSE_KEEPALIVE_SECONDS",
    "_ROUTES",
    "_ACTION_ORDER",
    "_compute_action_gating",
    "_open_db",
    "build_status",
    "generated_tests_for_work_item",
    "fetch_coverage_state",
    "fetch_task_detail",
    "fetch_blocker_detail",
    "_workflow_payload",
    "_is_under",
    "_content_type_header",
    "_parse_kind_filter",
    "_parse_json",
    "_FULL_MODE_OVERRIDE",
    "_FULL_MODE_OVERRIDE_EVENT",
    "_WRITE_DISABLED_MSG",
    "_CONFIG_WRITE_DISABLED_MSG",
    "set_full_mode_override",
    "is_full_mode_active",
    "_autonomy_writes_active",
    "_dashboard_write_settings",
    "_dashboard_config_write_settings",
    "_retention_sweep_on_startup",
    "_load_or_create_dashboard_token",
]
