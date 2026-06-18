"""Dashboard cockpit aggregations.

Issue #193 — overview API that backs the monitoring cockpit (planned vs
generated vs run metrics, current process, next operator action).

Issue #195 — chart-ready aggregations: run history, planned/generated/run
funnel, failure-kind trend, blocker/bug counters.

Issue #196 — structured `current_process` derived from unfinished runs.

Issue #199 — preflight panel that wraps `autonomy.preflight_check` and
augments it with dashboard-layer checks (runtime DB integrity, write
mode, dashboard port, model-CLI binaries).

The functions here are intentionally pure aggregations — they accept
`conn` + `paths` and return JSON-safe dicts. HTTP wiring lives in
``routes/dashboard_server.py`` behind the ``server.py`` compatibility facade.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import ConfigError, load_or_default
from .paths import RuntimePaths
from .storage.db import integrity_check
from .work_items import (
    annotate_spec_status,
    compute_candidate_debt,
    list_work_items,
    work_item_summary,
)


# Issue #193 — candidate decision buckets. Aligned with
# `_CANDIDATE_DECISION_KEYS` in work_items.py so the overview rolls up
# the same vocabulary the per-task debt chip uses.
_DECISION_KEYS = (
    "generate_now",
    "needs_operator_decision",
    "not_testable",
    "blocked_missing_docs",
)


def aggregate_candidate_debt(paths: RuntimePaths, items: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Sum candidate decision counts across all work items."""
    totals: Dict[str, int] = {key: 0 for key in _DECISION_KEYS}
    totals["total"] = 0
    for item in items:
        debt = compute_candidate_debt(paths, item.get("id"))
        for key in _DECISION_KEYS:
            totals[key] += int(debt.get(key, 0) or 0)
        totals["total"] += int(debt.get("total", 0) or 0)
    return totals


def count_generated_tests(conn: sqlite3.Connection, paths: RuntimePaths) -> Dict[str, Any]:
    """Count executable spec entries written by patch manifests.

    `work_item_artifacts.kind='apply'` proves a reviewed patch reached the
    working tree; it is not the same thing as generated executable tests.
    The v2 generator writes manifests under
    `agentic-os-runtime/patches/<task>/<run>/manifest.json`; each `files[]`
    entry is the durable generated-test source of truth for the cockpit.
    """
    counts: Dict[str, Any] = {
        "total": 0,
        "api": 0,
        "ui": 0,
        "other": 0,
        "manifests": 0,
        "source": "patch_manifests",
    }
    seen: set[str] = set()
    for manifest_path in sorted(paths.patches_dir.glob("*/*/manifest.json")):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        files = payload.get("files")
        if not isinstance(files, list):
            continue
        counts["manifests"] += 1
        for entry in files:
            if not isinstance(entry, dict):
                continue
            rel_path = str(entry.get("relative_path") or "").strip()
            if not rel_path or rel_path in seen:
                continue
            seen.add(rel_path)
            lower = rel_path.lower()
            counts["total"] += 1
            if "/api/" in lower or lower.endswith("_api.py") or "test_api" in lower:
                counts["api"] += 1
            elif "/ui/" in lower or "browser" in lower or "playwright" in lower:
                counts["ui"] += 1
            else:
                counts["other"] += 1
    return counts


def fetch_active_runs(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Runs that have started but not finished."""
    rows = conn.execute(
        """
        SELECT r.id          AS run_id,
               r.task_id     AS task_id,
               r.command     AS command,
               r.cwd         AS cwd,
               r.log_path    AS log_path,
               r.started_at  AS started_at,
               t.kind        AS task_kind,
               t.payload     AS task_payload,
               t.phase_id    AS phase_id
          FROM runs AS r
          LEFT JOIN tasks AS t ON t.id = r.task_id
         WHERE r.finished_at IS NULL
         ORDER BY r.started_at DESC;
        """
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_recent_runs(conn: sqlite3.Connection, *, limit: int = 30) -> List[Dict[str, Any]]:
    """Latest N finished runs for charts/trend."""
    rows = conn.execute(
        """
        SELECT id, task_id, command, exit_code, failure_kind,
               started_at, finished_at, duration_ms, manifest_path
          FROM runs
         WHERE finished_at IS NOT NULL
         ORDER BY finished_at DESC
         LIMIT ?;
        """,
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_bug_history(conn: sqlite3.Connection) -> Dict[str, int]:
    """Bug counts by status — feeds the blockers/bugs chart."""
    counts: Dict[str, int] = {"open": 0, "known": 0, "fixed": 0, "wont_fix": 0}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM bugs GROUP BY status;"
    ).fetchall()
    for row in rows:
        counts[row["status"]] = int(row["c"])
    counts["total"] = sum(counts.values())
    return counts


def fetch_blocker_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Open blockers by severity."""
    by_sev: Dict[str, int] = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    rows = conn.execute(
        """
        SELECT severity, COUNT(*) AS c
          FROM blockers
         WHERE status = 'open'
         GROUP BY severity;
        """
    ).fetchall()
    for row in rows:
        by_sev[row["severity"]] = int(row["c"])
    by_sev["total"] = sum(by_sev.values())
    return by_sev


def current_process(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """Most recent active run plus a stable shape for the UI.

    Returns None when nothing is in flight. The dashboard renders a
    `Current process` panel from this; the same payload is also reused
    by the task detail `Running now` block.
    """
    active = fetch_active_runs(conn)
    if not active:
        return None
    head = active[0]
    payload = {
        "run_id": head.get("run_id"),
        "task_id": head.get("task_id"),
        "task_kind": head.get("task_kind"),
        "phase_id": head.get("phase_id"),
        "command": head.get("command"),
        "started_at": head.get("started_at"),
        "log_path": head.get("log_path"),
        "active_count": len(active),
    }
    # Try to surface the work_item_id from the task payload — every
    # workflow that runs against a specific work item embeds it in the
    # payload via `payload["work_item_id"]` (see workflows.py).
    raw_payload = head.get("task_payload")
    if raw_payload:
        try:
            import json

            data = json.loads(raw_payload)
            if isinstance(data, dict):
                payload["work_item_id"] = data.get("work_item_id")
                payload["workflow"] = data.get("workflow")
        except (TypeError, ValueError):
            pass
    return payload


def next_operator_action(
    items: List[Dict[str, Any]],
    *,
    paths: Optional[RuntimePaths] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, str]]:
    """Pick the most actionable work item and describe the next step.

    The cockpit shows this so the operator never has to scan the whole
    queue to find what is waiting on them. Priority order:

    1. work items in `blocked` (audit-blocked, infra blocker) — fix first;
    2. `running` items with pending operator decisions on the plan;
    3. `analyzing/planning` items that need a decision to advance;
    4. oldest `queued` item if nothing else is pending.
    """
    if not items:
        return None
    blocked = [it for it in items if it.get("status") == "blocked"]
    debt = [it for it in items if (it.get("candidate_debt") or {}).get("needs_operator_decision", 0) > 0]
    queued = [it for it in items if it.get("status") == "queued"]
    if blocked:
        pick = blocked[0]
        return {
            "work_item_id": pick.get("id", ""),
            "title": pick.get("title", ""),
            "status": pick.get("status", ""),
            "action": "Unblock this task",
            "hint": "Open the task to review the blocker and decide next step.",
        }
    patch_rows: List[Dict[str, Any]] = []
    if paths is not None and conn is not None:
        try:
            from .gates import describe_blocking_patches

            patch_rows = describe_blocking_patches(paths, conn=conn)
        except Exception:
            patch_rows = []
    for state, action, hint in (
        (
            "approved_pending_apply",
            "Apply approved patch",
            "The review gate passed; apply the patch before running tests.",
        ),
        (
            "waiting",
            "Run review gate",
            "Generated tests are waiting for review before they can be applied.",
        ),
        (
            "rejected",
            "Resolve rejected patch",
            "Abandon the rejected patch or generate a corrected patch before continuing.",
        ),
    ):
        candidates = [p for p in patch_rows if p.get("state") == state and p.get("blocking")]
        if candidates:
            pick = candidates[0]
            return {
                "work_item_id": str(pick.get("work_item_id") or ""),
                "title": "",
                "status": str(pick.get("work_item_status") or ""),
                "action": action,
                "hint": hint,
            }
    if debt:
        pick = debt[0]
        return {
            "work_item_id": pick.get("id", ""),
            "title": pick.get("title", ""),
            "status": pick.get("status", ""),
            "action": "Review candidate decisions",
            "hint": "Approve or reject the pending test candidates.",
        }
    if queued:
        pick = queued[0]
        return {
            "work_item_id": pick.get("id", ""),
            "title": pick.get("title", ""),
            "status": pick.get("status", ""),
            "action": "Start analysis",
            "hint": "Kick off `analyze` on this queued task.",
        }
    return None


def fetch_budget_usage(conn: sqlite3.Connection, paths: RuntimePaths) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(tokens_in), 0) AS tokens_in,
          COALESCE(SUM(tokens_out), 0) AS tokens_out,
          COALESCE(SUM(cost_usd), 0.0) AS cost_usd
          FROM model_invocations;
        """
    ).fetchone()
    tokens_in = int(row["tokens_in"] or 0) if row is not None else 0
    tokens_out = int(row["tokens_out"] or 0) if row is not None else 0
    cost_usd = float(row["cost_usd"] or 0.0) if row is not None else 0.0
    try:
        cfg = load_or_default(paths.repo_root)
        budgets = cfg.raw.get("budgets") or {}
    except ConfigError:
        budgets = {}
    session = budgets.get("session") if isinstance(budgets, dict) else {}
    max_tokens = session.get("max_tokens") if isinstance(session, dict) else None
    max_usd = session.get("max_usd") if isinstance(session, dict) else None
    total_tokens = tokens_in + tokens_out
    token_ratio = (
        (total_tokens / max_tokens)
        if isinstance(max_tokens, int) and max_tokens > 0
        else None
    )
    cost_ratio = (
        (cost_usd / max_usd)
        if isinstance(max_usd, (int, float)) and max_usd > 0
        else None
    )
    ratios = [r for r in (token_ratio, cost_ratio) if r is not None]
    peak = max(ratios) if ratios else 0.0
    state = "ok"
    if peak >= 1:
        state = "exceeded"
    elif peak >= 0.8:
        state = "warn"
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "total_tokens": total_tokens,
        "cost_usd": round(cost_usd, 6),
        "limits": {
            "max_tokens": max_tokens,
            "max_usd": max_usd,
        },
        "ratio": round(peak, 4),
        "state": state,
    }


def build_overview(conn: sqlite3.Connection, paths: RuntimePaths) -> Dict[str, Any]:
    """The `/api/dashboard/overview` payload.

    Returns:
        work_items: queue counters by status (parity with /api/status.work_items)
        candidates: aggregated candidate debt across all work items
        generated_tests: counts of generated test artifacts (api/ui/other split)
        latest_run: structured summary of the most recent finished run
        current_process: active run details, or None
        next_action: prompt for the operator, or None
    """
    from .orchestrator import fetch_last_run

    raw_items = list_work_items(conn)
    items = annotate_spec_status(paths, raw_items)

    return {
        "work_items": work_item_summary(conn),
        "candidates": aggregate_candidate_debt(paths, items),
        "generated_tests": count_generated_tests(conn, paths),
        "latest_run": fetch_last_run(conn, paths),
        "current_process": current_process(conn),
        "next_action": next_operator_action(items, paths=paths, conn=conn),
        "bugs": fetch_bug_history(conn),
        "blockers": fetch_blocker_summary(conn),
        "budget": fetch_budget_usage(conn, paths),
    }


def build_charts(conn: sqlite3.Connection, paths: RuntimePaths) -> Dict[str, Any]:
    """The `/api/dashboard/charts` payload.

    Local, no remote deps. Arrays are pre-aggregated server-side; the
    front-end renders them as inline SVG.
    """
    runs = fetch_recent_runs(conn, limit=30)
    # Stacked bar source: per-run pass/fail/skip from reports/last-run is
    # only available for the most recent run; for older runs we only have
    # exit_code + failure_kind. Surface what we have honestly.
    history: List[Dict[str, Any]] = []
    failure_trend: Dict[str, int] = {"green": 0, "product": 0, "infra": 0, "timeout": 0, "unknown": 0}
    for r in runs:
        exit_code = r.get("exit_code")
        fk = r.get("failure_kind")
        outcome = "green"
        if exit_code == 0:
            outcome = "green"
        elif fk == "product":
            outcome = "product"
        elif fk == "infra":
            outcome = "infra"
        elif fk == "timeout":
            outcome = "timeout"
        elif fk:
            outcome = fk
        else:
            outcome = "unknown"
        history.append({
            "run_id": r.get("id"),
            "task_id": r.get("task_id"),
            "finished_at": r.get("finished_at"),
            "duration_ms": r.get("duration_ms"),
            "outcome": outcome,
            "exit_code": exit_code,
        })
        failure_trend[outcome] = failure_trend.get(outcome, 0) + 1

    raw_items = list_work_items(conn)
    items = annotate_spec_status(paths, raw_items)
    candidates = aggregate_candidate_debt(paths, items)
    generated = count_generated_tests(conn, paths)

    # Planned -> approved -> generated -> run funnel.
    planned = candidates.get("total", 0)
    approved = candidates.get("generate_now", 0)
    generated_total = generated.get("total", 0)
    # "run" approximated as candidates whose work item has a recent run.
    runs_with_wi = conn.execute(
        """
        SELECT COUNT(DISTINCT wia.work_item_id) AS c
          FROM work_item_artifacts AS wia
         WHERE wia.kind = 'run';
        """
    ).fetchone()
    runs_count = int(runs_with_wi["c"]) if runs_with_wi else 0

    return {
        "run_history": history,
        "failure_trend": failure_trend,
        "funnel": {
            "planned": planned,
            "approved": approved,
            "generated": generated_total,
            "run_against_work_items": runs_count,
        },
        "bugs": fetch_bug_history(conn),
        "blockers": fetch_blocker_summary(conn),
    }


def _port_free(host: str, port: int) -> bool:
    """Whether ``host:port`` is bindable. Used by the preflight panel."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
            return True
    except OSError:
        return False


def build_preflight(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    *,
    write_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """The `/api/dashboard/preflight` payload.

    Wraps `autonomy.preflight_check` (which already covers config, SUT
    root/discovery, Docker, model CLIs, test runner, healthcheck targets,
    runtime DB existence) and augments it with dashboard-layer checks:

    * runtime DB integrity (`PRAGMA integrity_check`);
    * write-mode visibility (so the operator knows if action buttons will
      actually take effect).

    Each check has `id`, `status` (`pass`|`warn`|`fail`), `message`, and
    `actions` (operator-facing remediation hints).
    """
    from .autonomy import preflight_check as _autonomy_preflight

    payload = _autonomy_preflight(paths)
    checks: List[Dict[str, Any]] = list(payload.get("checks") or [])

    # Runtime DB integrity.
    db_state = integrity_check(conn)
    checks.append(
        {
            "id": "runtime_db_integrity",
            "status": "pass" if db_state == "ok" else "fail",
            "message": "runtime DB integrity ok" if db_state == "ok"
                       else f"runtime DB integrity check returned: {db_state}",
            "actions": [] if db_state == "ok" else [
                "Stop the dashboard, back up `agentic-os-runtime/runtime.db`, then run `./scripts/agentic-os.sh run recover`.",
            ],
        }
    )

    # Write-mode visibility — operators need to know if action buttons
    # are inert (read-only mode) before they try to start a run.
    if write_enabled is None:
        write_mode_env = os.environ.get("AGENTIC_OS_DASHBOARD_WRITE_ENABLED", "").strip().lower()
        env_writable = write_mode_env in {"1", "true", "yes", "on"}
        config_writable = False
        try:
            from .config import load_or_default

            cfg = load_or_default(paths.repo_root)
            config_writable = bool(
                (cfg.raw.get("dashboard") or {}).get("enable_write_endpoints")
            )
        except Exception:
            config_writable = False
        is_writable = env_writable or config_writable
    else:
        is_writable = bool(write_enabled)
    checks.append(
        {
            "id": "dashboard_write_mode",
            "status": "pass" if is_writable else "warn",
            "message": "dashboard writes enabled" if is_writable
                       else "dashboard is read-only (no action buttons will execute)",
            "actions": [] if is_writable else [
                "Restart with `./scripts/agentic-os.sh up --dashboard-only --foreground --full` to enable writes for this session.",
            ],
        }
    )

    ok = all(c.get("status") != "fail" for c in checks)
    warn = any(c.get("status") == "warn" for c in checks)
    return {"ok": ok, "warn": warn and ok, "checks": checks}
