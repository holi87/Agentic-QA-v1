"""Provider-chain ranking and swap-success recording.

Split from models/__init__.py (issue #292).
"""
from __future__ import annotations

from typing import Optional

from ..events import EventLog


def _rank_chain_by_quality(conn, events: EventLog, *, role: str, chain: list) -> list:
    """Re-order the live provider chain by historical quality (issue #273).

    Advisory: a stable sort by the `provider_quality` learning weight floats
    recently-confirmed providers ahead of the rest while preserving config
    order for ties. With no learnings the order is unchanged, so default
    behaviour is preserved. Any failure returns the chain untouched — a hint
    must never break invocation. Emits `learning.consulted` when applied.
    """
    try:
        from ..learnings import provider_quality_scores

        scores = provider_quality_scores(conn, role=role)
    except Exception:
        return chain
    if not scores or len(chain) < 2:
        return chain
    ranked = sorted(
        chain, key=lambda e: scores.get(str(e.get("provider", "")), 0.0), reverse=True
    )
    if [e.get("provider") for e in ranked] != [e.get("provider") for e in chain]:
        try:
            events.write(
                "learning.consulted",
                actor=f"{role}-router",
                payload={
                    "kind": "provider_quality",
                    "role": role,
                    "scores": scores,
                    "order": [e.get("provider") for e in ranked],
                },
            )
        except Exception:
            pass
    return ranked


def _record_swap_success(
    conn, *, role: str, provider: str, rescued_from: Optional[str], trigger: Optional[str]
) -> None:
    """Note that a fallback provider rescued a failover (issue #273).

    Advisory `provider_quality` learning so the router prefers this provider
    for the role next time. Best-effort: never raises into the call path.
    """
    try:
        from ..learnings import record_learning

        record_learning(
            conn,
            kind="provider_quality",
            subject=f"{role}::{provider}",
            payload={"rescued_from": rescued_from, "trigger": trigger},
            actor=f"{role}-failover",
        )
    except Exception:
        pass
