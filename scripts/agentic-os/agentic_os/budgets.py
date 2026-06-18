"""Lightweight model budget aggregation helpers.

This module is intentionally independent from ``agentic_os.models`` so CLI and
dashboard budget polling can read usage without importing provider/failover
machinery in request-handler threads.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def estimate_tokens(text: str) -> int:
    """Cheap, provider-agnostic token estimate (~4 chars/token).

    Shared by model accounting and prompt-context budgeting (issue #293) so
    both use one heuristic. Always returns at least 1 for non-empty text.
    """
    return max(1, (len(text) + 3) // 4)


def _token_total(
    conn,
    *,
    session_id: Optional[str] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    role: Optional[str] = None,
) -> int:
    clauses = []
    params: list[Any] = []
    if run_id is not None:
        clauses.append("run_id=?")
        params.append(run_id)
    if session_id is not None:
        clauses.append("session_id=?")
        params.append(session_id)
    if task_id is not None:
        clauses.append("task_id=?")
        params.append(task_id)
    if role is not None:
        clauses.append("model_role=?")
        params.append(role)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)), 0) AS total "
        f"FROM model_invocations{where};",
        params,
    ).fetchone()
    return int(row["total"] if row else 0)


def _cost_total(
    conn,
    *,
    session_id: Optional[str] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    role: Optional[str] = None,
) -> float:
    clauses = []
    params: list[Any] = []
    if run_id is not None:
        clauses.append("run_id=?")
        params.append(run_id)
    if session_id is not None:
        clauses.append("session_id=?")
        params.append(session_id)
    if task_id is not None:
        clauses.append("task_id=?")
        params.append(task_id)
    if role is not None:
        clauses.append("model_role=?")
        params.append(role)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(cost_usd, 0)), 0) AS total "
        f"FROM model_invocations{where};",
        params,
    ).fetchone()
    return float(row["total"] if row else 0.0)


def budget_status(
    conn,
    budgets: Optional[Dict[str, Any]],
    *,
    session_id: Optional[str] = None,
    models: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return live token/USD consumption vs configured limits.

    The payload is shared by ``budget show`` and ``/api/budget/status``.
    ``budgets.per_role`` is keyed by config slot (planner/implementer/reviewer/
    triager) while ``model_invocations.model_role`` stores the resolved model
    role (opus/sonnet/etc.). The ``models`` map bridges the two.
    """
    budgets = budgets if isinstance(budgets, dict) else {}
    models = models if isinstance(models, dict) else {}
    session_cfg = budgets.get("session") or {}
    max_tokens = session_cfg.get("max_tokens") if isinstance(session_cfg, dict) else None
    max_usd = session_cfg.get("max_usd") if isinstance(session_cfg, dict) else None

    session_tokens = _token_total(conn, session_id=session_id)
    session_cost = _cost_total(conn, session_id=session_id)

    def _pct(current: float, limit: Any) -> Optional[float]:
        if not isinstance(limit, (int, float)) or limit <= 0:
            return None
        return round(100.0 * current / float(limit), 2)

    per_role = budgets.get("per_role") if isinstance(budgets.get("per_role"), dict) else {}
    limit_by_model_role: Dict[str, Any] = {}
    for slot, limits in per_role.items():
        model_role = (models.get(slot) or {}).get("role") if isinstance(models.get(slot), dict) else None
        if model_role and isinstance(limits, dict) and limits.get("max_tokens") is not None:
            limit_by_model_role[model_role] = limits.get("max_tokens")

    roles: list[Dict[str, Any]] = []
    rows = conn.execute(
        "SELECT model_role AS role, "
        "COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) AS tokens, "
        "COALESCE(SUM(COALESCE(cost_usd,0)),0) AS cost "
        + (
            "FROM model_invocations WHERE session_id=? GROUP BY model_role;"
            if session_id else
            "FROM model_invocations GROUP BY model_role;"
        ),
        ((session_id,) if session_id else ()),
    ).fetchall()
    usage_by_role = {r["role"]: (int(r["tokens"]), float(r["cost"])) for r in rows}
    for role in sorted(set(limit_by_model_role.keys()) | set(usage_by_role.keys())):
        tokens, cost = usage_by_role.get(role, (0, 0.0))
        limit = limit_by_model_role.get(role)
        roles.append({
            "role": role,
            "tokens": tokens,
            "cost_usd": round(cost, 6),
            "max_tokens": limit,
            "pct": _pct(tokens, limit),
        })

    return {
        "session_id": session_id,
        "fail_mode": budgets.get("fail_mode", "abort"),
        "session": {
            "tokens": session_tokens,
            "max_tokens": max_tokens,
            "tokens_pct": _pct(session_tokens, max_tokens),
            "cost_usd": round(session_cost, 6),
            "max_usd": max_usd,
            "usd_pct": _pct(session_cost, max_usd),
        },
        "per_role": roles,
    }
