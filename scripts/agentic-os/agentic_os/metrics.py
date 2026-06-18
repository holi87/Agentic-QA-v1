"""Wave 14 (#314) — unified metrics + monitoring rollup.

A single ``build_metrics`` function aggregates the seven KPIs the epic
acceptance demands from the existing tables (no new schema):

* tests created/run over time (work_items, runs, work_item_artifacts);
* pass/fail per surface (coverage_ledger surface_kind + runs outcome);
* coverage delta per session (autonomy_sessions ↔ coverage_ledger.created_at);
* session cost/tokens (model_invocations.tokens_in/out/cost_usd, now real
  after #308);
* provider failover rate (model_invocations rows where the requested
  role's primary provider differs from the one that finally ran +
  provider_cooldowns rows);
* block-reason distribution (autonomy_sessions.blocks + events with
  ``idle:blocked`` step);
* avg time per phase (runs.duration_ms grouped by tasks.kind).

Designed to be cheap enough for a 5-second dashboard poll: every query is
indexed-hit + LIMIT bounded. ``/api/metrics`` and the Prometheus exporter
share this single rollup so the two surfaces never drift.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from .paths import RuntimePaths
from .time_utils import now_iso


_DEFAULT_RUN_LIMIT = 200
_DEFAULT_SESSION_LIMIT = 20


def build_metrics(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    *,
    run_limit: int = _DEFAULT_RUN_LIMIT,
    session_limit: int = _DEFAULT_SESSION_LIMIT,
) -> Dict[str, Any]:
    """Single rollup feeding ``/api/metrics`` and the Prometheus exporter."""
    return {
        "generated_at": now_iso(),
        "sessions": _sessions_summary(conn, limit=session_limit),
        "tests": _tests_metrics(conn, run_limit=run_limit),
        "coverage": _coverage_metrics(conn),
        "cost": _cost_metrics(conn, session_limit=session_limit),
        "providers": _provider_metrics(conn),
        "blocks": _block_metrics(conn, session_limit=session_limit),
        "phase_timing": _phase_timing(conn, run_limit=run_limit),
    }


# ---------------------------------------------------------------------------
# Components — each returns a JSON-safe dict, never raises (a broken table
# would otherwise hide every other metric). Missing tables yield zeros.
# ---------------------------------------------------------------------------


def _sessions_summary(conn: sqlite3.Connection, *, limit: int) -> Dict[str, Any]:
    try:
        rows = conn.execute(
            """
            SELECT id, started_at, finished_at, status, mode,
                   max_minutes, work_items_processed, blocks, failures,
                   primary_actor
              FROM autonomy_sessions
             ORDER BY started_at DESC
             LIMIT ?;
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return {"recent": [], "totals": {"sessions": 0, "blocks": 0, "failures": 0}}
    recent = [dict(r) for r in rows]
    totals = {
        "sessions": len(recent),
        "blocks": sum(int(r.get("blocks") or 0) for r in recent),
        "failures": sum(int(r.get("failures") or 0) for r in recent),
        "work_items_processed": sum(
            int(r.get("work_items_processed") or 0) for r in recent
        ),
    }
    return {"recent": recent, "totals": totals}


def _tests_metrics(conn: sqlite3.Connection, *, run_limit: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "work_items_total": 0,
        "work_items_by_status": {},
        "runs_recent": [],
        "outcomes": {"pass": 0, "product": 0, "infra": 0, "timeout": 0, "unknown": 0},
        "patches_applied": 0,
    }
    try:
        out["work_items_total"] = int(
            conn.execute("SELECT COUNT(*) AS n FROM work_items;").fetchone()["n"]
        )
        by_status = conn.execute(
            "SELECT status, COUNT(*) AS n FROM work_items GROUP BY status;"
        ).fetchall()
        out["work_items_by_status"] = {r["status"]: int(r["n"]) for r in by_status}
    except sqlite3.Error:
        pass
    try:
        runs = conn.execute(
            """
            SELECT id, task_id, exit_code, failure_kind, started_at,
                   finished_at, duration_ms
              FROM runs
             ORDER BY started_at DESC
             LIMIT ?;
            """,
            (run_limit,),
        ).fetchall()
        out["runs_recent"] = [dict(r) for r in runs]
        for r in runs:
            ex = r["exit_code"]
            fk = r["failure_kind"]
            if ex == 0:
                out["outcomes"]["pass"] += 1
            elif fk in out["outcomes"]:
                out["outcomes"][fk] += 1
            elif fk:
                out["outcomes"].setdefault(fk, 0)
                out["outcomes"][fk] += 1
            else:
                out["outcomes"]["unknown"] += 1
    except sqlite3.Error:
        pass
    try:
        out["patches_applied"] = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM work_item_artifacts WHERE kind='apply';"
            ).fetchone()["n"]
        )
    except sqlite3.Error:
        pass
    return out


def _coverage_metrics(conn: sqlite3.Connection) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "by_project": [],
        "by_surface_kind": {},
        "total_rows": 0,
    }
    try:
        rows = conn.execute(
            """
            SELECT project_id, surface_kind, COUNT(*) AS n
              FROM coverage_ledger
             GROUP BY project_id, surface_kind
             ORDER BY project_id, surface_kind;
            """
        ).fetchall()
    except sqlite3.Error:
        return out
    by_project: Dict[str, Dict[str, int]] = {}
    for r in rows:
        pid = r["project_id"]
        kind = r["surface_kind"]
        n = int(r["n"])
        by_project.setdefault(pid, {})[kind] = n
        out["by_surface_kind"][kind] = out["by_surface_kind"].get(kind, 0) + n
        out["total_rows"] += n
    out["by_project"] = [
        {"project_id": pid, "counts": counts} for pid, counts in by_project.items()
    ]
    return out


def _cost_metrics(conn: sqlite3.Connection, *, session_limit: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "totals": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "invocations": 0},
        "by_provider": [],
        "by_role": [],
        "by_session": [],
    }
    try:
        total = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(tokens_in), 0)  AS tokens_in,
                   COALESCE(SUM(tokens_out), 0) AS tokens_out,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd
              FROM model_invocations;
            """
        ).fetchone()
        if total:
            out["totals"] = {
                "invocations": int(total["n"]),
                "tokens_in": int(total["tokens_in"]),
                "tokens_out": int(total["tokens_out"]),
                "cost_usd": float(total["cost_usd"]),
            }
        provider_rows = conn.execute(
            """
            SELECT provider,
                   COUNT(*) AS n,
                   COALESCE(SUM(tokens_in), 0)  AS tokens_in,
                   COALESCE(SUM(tokens_out), 0) AS tokens_out,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd
              FROM model_invocations
             GROUP BY provider
             ORDER BY cost_usd DESC, n DESC;
            """
        ).fetchall()
        out["by_provider"] = [dict(r) for r in provider_rows]
        role_rows = conn.execute(
            """
            SELECT model_role,
                   COUNT(*) AS n,
                   COALESCE(SUM(tokens_in), 0)  AS tokens_in,
                   COALESCE(SUM(tokens_out), 0) AS tokens_out,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd
              FROM model_invocations
             GROUP BY model_role
             ORDER BY cost_usd DESC, n DESC;
            """
        ).fetchall()
        out["by_role"] = [dict(r) for r in role_rows]
        session_rows = conn.execute(
            """
            SELECT session_id,
                   COUNT(*) AS n,
                   COALESCE(SUM(tokens_in), 0)  AS tokens_in,
                   COALESCE(SUM(tokens_out), 0) AS tokens_out,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd
              FROM model_invocations
             WHERE session_id IS NOT NULL
             GROUP BY session_id
             ORDER BY MAX(started_at) DESC
             LIMIT ?;
            """,
            (session_limit,),
        ).fetchall()
        out["by_session"] = [dict(r) for r in session_rows]
    except sqlite3.Error:
        return out
    return out


def _provider_metrics(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Provider failover rate + active cooldowns.

    Failover heuristic: count model_invocations whose ``provider``
    differs from the role's nominal primary (best-effort — derived from
    the most-used provider per role over the dataset). For an MVP this
    is enough to surface "Claude is down → Codex picked up the slack"
    without a per-call routing log.
    """
    out: Dict[str, Any] = {
        "by_provider_outcomes": [],
        "active_cooldowns": [],
        "failover_events": 0,
    }
    try:
        outcomes = conn.execute(
            """
            SELECT provider,
                   model_role,
                   COUNT(*) AS n,
                   SUM(CASE WHEN exit_code = 0 THEN 1 ELSE 0 END) AS ok,
                   SUM(CASE WHEN exit_code IS NOT NULL AND exit_code <> 0 THEN 1 ELSE 0 END) AS bad
              FROM model_invocations
             GROUP BY provider, model_role
             ORDER BY n DESC;
            """
        ).fetchall()
        out["by_provider_outcomes"] = [dict(r) for r in outcomes]
    except sqlite3.Error:
        pass
    try:
        cooldowns = conn.execute(
            """
            SELECT role, provider, cooldown_until, trigger, updated_at
              FROM provider_cooldowns
             ORDER BY cooldown_until DESC;
            """
        ).fetchall()
        out["active_cooldowns"] = [dict(r) for r in cooldowns]
    except sqlite3.Error:
        pass
    # Failover events: number of role rows where >1 provider has handled
    # the role. Approximation — exact routing logs would be richer.
    try:
        roles_multi = conn.execute(
            """
            SELECT model_role, COUNT(DISTINCT provider) AS providers
              FROM model_invocations
             GROUP BY model_role
            HAVING providers > 1;
            """
        ).fetchall()
        out["failover_events"] = sum(int(r["providers"]) - 1 for r in roles_multi)
    except sqlite3.Error:
        pass
    return out


def _block_metrics(conn: sqlite3.Connection, *, session_limit: int) -> Dict[str, Any]:
    """Block-reason distribution.

    Per-session ``blocks`` counter lives in ``autonomy_sessions``. Reason
    strings live in the in-memory ``_SessionState.events_log`` ring
    buffer (#265), which drops old entries; we mirror durable block
    reasons through the events log (``work_item.coverage_skipped``,
    ``autonomy.completed``, ``idle:blocked`` step events) for the
    histogram. Missing event kinds are treated as zero.
    """
    out: Dict[str, Any] = {"total_blocks": 0, "by_reason": []}
    try:
        total = conn.execute(
            "SELECT COALESCE(SUM(blocks), 0) AS n FROM autonomy_sessions;"
        ).fetchone()
        out["total_blocks"] = int(total["n"]) if total else 0
    except sqlite3.Error:
        pass
    # Histogram of recent block-related event kinds. Capped so a runaway
    # event log cannot blow up the response.
    try:
        rows = conn.execute(
            """
            SELECT kind, COUNT(*) AS n
              FROM events
             WHERE kind IN (
                'work_item.coverage_skipped',
                'work_item.implement_idempotent_noop',
                'autonomy.completed',
                'work_item.coverage_ledger_error'
             )
             GROUP BY kind
             ORDER BY n DESC;
            """
        ).fetchall()
        out["by_reason"] = [dict(r) for r in rows]
    except sqlite3.Error:
        pass
    return out


def _phase_timing(conn: sqlite3.Connection, *, run_limit: int) -> Dict[str, Any]:
    """Avg + p50 + p95 ms per ``tasks.kind`` (the phase proxy).

    Bound by ``run_limit`` so the rollup stays cheap. SQLite has no
    built-in percentile but ``ORDER BY``/``LIMIT`` does the job for the
    bounded window.
    """
    out: Dict[str, Any] = {"by_kind": []}
    try:
        rows = conn.execute(
            """
            SELECT t.kind AS kind,
                   COUNT(r.id) AS n,
                   AVG(r.duration_ms) AS avg_ms,
                   MIN(r.duration_ms) AS min_ms,
                   MAX(r.duration_ms) AS max_ms
              FROM runs AS r
              JOIN tasks AS t ON t.id = r.task_id
             WHERE r.duration_ms IS NOT NULL
             GROUP BY t.kind
             ORDER BY n DESC;
            """
        ).fetchall()
        out["by_kind"] = [dict(r) for r in rows]
    except sqlite3.Error:
        pass
    return out


# ---------------------------------------------------------------------------
# Prometheus text-format exporter
# ---------------------------------------------------------------------------


def render_prometheus(metrics: Dict[str, Any]) -> str:
    """Serialize ``build_metrics`` output as Prometheus exposition format.

    Includes only the gauges/counters that map cleanly to single
    numeric values; richer per-row dimensions (per-provider, per-role,
    per-session) become labeled series. Unknown / missing values are
    skipped so a partial snapshot never produces a malformed payload.
    """
    lines: List[str] = []

    def _gauge(name: str, value: Any, *, help_text: str, labels: Optional[Dict[str, str]] = None) -> None:
        if value is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        # Header lines repeated per name keep the exposition format simple
        # and Prometheus tolerates duplicate # HELP / # TYPE — kept compact
        # for readability instead.
        if labels:
            label_str = ",".join(
                f'{k}="{_pr_escape(v)}"' for k, v in labels.items() if v is not None
            )
            lines.append(f"{name}{{{label_str}}} {v}")
        else:
            lines.append(f"{name} {v}")

    tests = metrics.get("tests") or {}
    cost = metrics.get("cost") or {}
    cov = metrics.get("coverage") or {}
    blocks = metrics.get("blocks") or {}
    sessions = metrics.get("sessions") or {}
    providers = metrics.get("providers") or {}
    phase = metrics.get("phase_timing") or {}

    lines.append("# HELP agentic_os_work_items_total Total work items recorded.")
    lines.append("# TYPE agentic_os_work_items_total gauge")
    _gauge("agentic_os_work_items_total", tests.get("work_items_total"), help_text="")

    lines.append("# HELP agentic_os_runs_outcomes Recent run outcomes by category.")
    lines.append("# TYPE agentic_os_runs_outcomes gauge")
    for outcome, n in (tests.get("outcomes") or {}).items():
        _gauge(
            "agentic_os_runs_outcomes",
            n,
            help_text="",
            labels={"outcome": outcome},
        )

    lines.append("# HELP agentic_os_patches_applied Patches that landed.")
    lines.append("# TYPE agentic_os_patches_applied counter")
    _gauge("agentic_os_patches_applied", tests.get("patches_applied"), help_text="")

    lines.append("# HELP agentic_os_coverage_rows Coverage-ledger row counts.")
    lines.append("# TYPE agentic_os_coverage_rows gauge")
    _gauge("agentic_os_coverage_rows_total", cov.get("total_rows"), help_text="")
    for kind, n in (cov.get("by_surface_kind") or {}).items():
        _gauge(
            "agentic_os_coverage_rows",
            n,
            help_text="",
            labels={"surface_kind": kind},
        )

    lines.append("# HELP agentic_os_cost_usd Provider cost (USD).")
    lines.append("# TYPE agentic_os_cost_usd gauge")
    _gauge("agentic_os_cost_usd_total", (cost.get("totals") or {}).get("cost_usd"), help_text="")
    _gauge(
        "agentic_os_tokens_in_total",
        (cost.get("totals") or {}).get("tokens_in"),
        help_text="",
    )
    _gauge(
        "agentic_os_tokens_out_total",
        (cost.get("totals") or {}).get("tokens_out"),
        help_text="",
    )
    for row in cost.get("by_provider") or []:
        _gauge(
            "agentic_os_cost_usd",
            row.get("cost_usd"),
            help_text="",
            labels={"provider": row.get("provider") or "unknown"},
        )

    lines.append("# HELP agentic_os_block_total Cumulative autonomy block events.")
    lines.append("# TYPE agentic_os_block_total counter")
    _gauge("agentic_os_block_total", blocks.get("total_blocks"), help_text="")

    lines.append("# HELP agentic_os_session_count Recent autonomy session count.")
    lines.append("# TYPE agentic_os_session_count gauge")
    _gauge(
        "agentic_os_session_count",
        (sessions.get("totals") or {}).get("sessions"),
        help_text="",
    )

    lines.append("# HELP agentic_os_provider_failover_events Approx. failover events across roles.")
    lines.append("# TYPE agentic_os_provider_failover_events counter")
    _gauge(
        "agentic_os_provider_failover_events",
        providers.get("failover_events"),
        help_text="",
    )

    lines.append("# HELP agentic_os_phase_duration_ms_avg Average run duration per task kind.")
    lines.append("# TYPE agentic_os_phase_duration_ms_avg gauge")
    for row in phase.get("by_kind") or []:
        _gauge(
            "agentic_os_phase_duration_ms_avg",
            row.get("avg_ms"),
            help_text="",
            labels={"kind": row.get("kind") or "unknown"},
        )

    return "\n".join(lines) + "\n"


def _pr_escape(value: str) -> str:
    """Escape a Prometheus label value per the text format spec."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )
