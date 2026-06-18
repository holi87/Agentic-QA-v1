"""Token estimation and budget pre-flight checks for model invocation.

Split from models/__init__.py (issue #292).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from ..budgets import _cost_total, _token_total
from ..errors import BudgetExceededError
from ..events import EventLog


def _estimate_tokens(text: str) -> int:
    # Single heuristic shared with prompt-context budgeting (issue #293).
    from ..budgets import estimate_tokens

    return estimate_tokens(text)


def _check_budget_before_call(
    conn,
    events: EventLog,
    budgets: Dict[str, Any],
    *,
    role: str,
    session_id: Optional[str],
    task_id: Optional[str],
    run_id: Optional[str],
    estimated_tokens: int,
    repo_root: Optional[Path] = None,
    provider: Optional[str] = None,
) -> None:
    if not budgets:
        return
    fail_mode = budgets.get("fail_mode", "abort")
    checks: list[tuple[str, Optional[int], int]] = []
    session_budget = ((budgets.get("session") or {}).get("max_tokens"))
    if isinstance(session_budget, int):
        checks.append(("session", session_budget, _token_total(conn, session_id=session_id)))
    role_budget = (((budgets.get("per_role") or {}).get(role) or {}).get("max_tokens"))
    if isinstance(role_budget, int):
        checks.append(("role", role_budget, _token_total(conn, run_id=run_id, role=role)))
    work_item_budget = ((budgets.get("per_work_item") or {}).get("max_tokens"))
    if isinstance(work_item_budget, int) and task_id:
        checks.append(("work_item", work_item_budget, _token_total(conn, task_id=task_id)))
    for dimension, limit, current in checks:
        if limit is not None and current + estimated_tokens > limit:
            payload = {
                "dimension": dimension,
                "limit": limit,
                "current_tokens": current,
                "estimated_next_call_tokens": estimated_tokens,
            }
            events.write("budget.exceeded", severity="error", task_id=task_id, payload=payload)
            if fail_mode == "warn":
                continue
            raise BudgetExceededError(
                f"model token budget exceeded for {dimension}: "
                f"{current}+{estimated_tokens}>{limit}"
            )

    # Codex PR #275 review (P2) — also enforce the configured USD cap.
    # Schema currently exposes max_usd only at session scope; we still emit
    # the same `budget.exceeded` event so dashboards/alerts pick it up.
    session_usd_limit = ((budgets.get("session") or {}).get("max_usd"))
    if isinstance(session_usd_limit, (int, float)) and repo_root is not None and provider:
        current_cost = _cost_total(conn, session_id=session_id)
        estimated_cost = _cost_usd(
            repo_root, provider, tokens_in=estimated_tokens, tokens_out=0
        )
        projected = round(current_cost + estimated_cost, 6)
        if projected > float(session_usd_limit):
            payload = {
                "dimension": "session_usd",
                "limit": float(session_usd_limit),
                "current_usd": current_cost,
                "estimated_next_call_usd": estimated_cost,
            }
            events.write("budget.exceeded", severity="error", task_id=task_id, payload=payload)
            if fail_mode != "warn":
                raise BudgetExceededError(
                    f"model usd budget exceeded for session: "
                    f"{current_cost:.6f}+{estimated_cost:.6f}>{float(session_usd_limit):.6f}"
                )


def _tokens_from_envelope(envelope, *, fallback_in: int, fallback_out: int) -> tuple[int, int]:
    metadata = envelope.metadata if envelope is not None else {}
    tokens_in = metadata.get("tokens_in")
    tokens_out = metadata.get("tokens_out")
    if not isinstance(tokens_in, int) or tokens_in < 0:
        tokens_in = fallback_in
    if not isinstance(tokens_out, int) or tokens_out < 0:
        tokens_out = fallback_out
    return tokens_in, tokens_out


def _cost_usd(repo_root: Path, provider: str, *, tokens_in: int, tokens_out: int) -> float:
    rates = _provider_rates(repo_root).get(provider) or {}
    in_rate = rates.get("input_per_1k_usd", 0.0)
    out_rate = rates.get("output_per_1k_usd", 0.0)
    if not isinstance(in_rate, (int, float)) or not isinstance(out_rate, (int, float)):
        return 0.0
    return round((tokens_in / 1000.0) * float(in_rate) + (tokens_out / 1000.0) * float(out_rate), 6)


def _provider_rates(repo_root: Path) -> Dict[str, Any]:
    path = repo_root / "config" / "provider-rates.yml"
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}
