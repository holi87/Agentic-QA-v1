"""Inject distilled cross-run learnings into agent prompts (issue #287).

The #273 learnings store holds advisory hints distilled from history. This
module renders the *relevant* live learnings into a compact, caveman-
compressible block that ``models._invoke_attempt`` prepends to planner and
implementer prompts (after the #293 architecture block), so the agent starts
with the project's hard-won memory instead of re-discovering it.

Design notes (mirrors ``architecture_context``):
- Pure reads from the learnings store; this module never emits events. The
  caller emits the ``learning.consulted`` audit event when a block applies.
- Budget-bounded via ``budgets.estimate_tokens`` so prompt-context injections
  share one heuristic with model accounting.
- Best-effort: any failure here must be swallowed by the caller; ``None`` means
  "nothing relevant to inject", not an error. Learnings are HINTS only — the
  gates still decide. A wrong hint must never break invocation.
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from .budgets import estimate_tokens

DEFAULT_BUDGET_TOKENS = 400

# Roles that receive the learnings block. The reviewer/triager paths read the
# store directly at their decision sites; the planner/implementer get the hint
# up front so they avoid known traps.
_INJECT_ROLES = ("planner", "implementer")


def relevant_learnings(conn: sqlite3.Connection) -> dict:
    """Collect the live learnings relevant to a planner/implementer prompt.

    Returns a dict of lists keyed by kind. Pure read; never raises for an
    empty store (returns empty lists). Callers wrap this best-effort.
    """
    from .learnings import (
        coverage_gap_subjects,
        flaky_subjects,
        skill_failure_subjects,
    )

    return {
        "flaky": list(flaky_subjects(conn)),
        "coverage_gap": list(coverage_gap_subjects(conn)),
        "skill_failure": list(skill_failure_subjects(conn)),
    }


def _render_block(data: dict) -> Optional[str]:
    flaky: List[str] = data.get("flaky") or []
    coverage_gap = data.get("coverage_gap") or []
    skill_failure = data.get("skill_failure") or []
    if not (flaky or coverage_gap or skill_failure):
        return None

    lines: List[str] = [
        "## Learnings (cross-run hints — apply, do not echo)",
        "",
        "Advisory memory distilled from prior runs. Gates still decide; treat "
        "these as priors:",
        "",
    ]
    if flaky:
        lines.append("- Flaky (quarantine; do not gate the green path on these):")
        lines.extend(f"  - {s}" for s in flaky)
    if coverage_gap:
        lines.append("- Coverage gaps (recurring; ensure these are covered):")
        for item in coverage_gap:
            subject = item.get("subject", "")
            missing = item.get("payload", {}).get("missing")
            if missing:
                lines.append(f"  - {subject} (missing: {', '.join(str(m) for m in missing)})")
            else:
                lines.append(f"  - {subject}")
    if skill_failure:
        lines.append("- Repeated reject clusters (pre-empt these reasons):")
        for item in skill_failure:
            subject = item.get("subject", "")
            reason = item.get("payload", {}).get("reason")
            lines.append(f"  - {subject}" + (f" ({reason})" if reason else ""))
    return "\n".join(lines) + "\n"


def _bound_to_budget(block: str, budget_tokens: int) -> str:
    """Trim the block to the token budget on a line boundary if needed."""
    if budget_tokens <= 0 or estimate_tokens(block) <= budget_tokens:
        return block
    # estimate_tokens ~= chars/4; keep headroom for the wrapper.
    max_chars = max(0, budget_tokens * 4)
    if len(block) <= max_chars:
        return block
    trimmed = block[:max_chars].rsplit("\n", 1)[0].rstrip()
    return trimmed + "\n  …\n"


def learnings_context_block(
    conn: sqlite3.Connection,
    *,
    role: str,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> Optional[str]:
    """Return the prompt-ready learnings block, or None when nothing applies.

    ``None`` means "skip injection" — no live learnings, an unsupported role,
    or an empty render. The caller prepends the returned string and emits the
    ``learning.consulted`` audit event.
    """
    if role not in _INJECT_ROLES:
        return None
    data = relevant_learnings(conn)
    block = _render_block(data)
    if block is None:
        return None
    return _bound_to_budget(block, budget_tokens)
