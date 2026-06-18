"""Inject per-project prior context into agent prompts (issue #289).

The #289 ``memory_index`` (FTS5) holds a project's distilled history — session
summaries, transcripts, bugs, decisions, learnings. This module queries the
*active* project's memory for the snippets most relevant to the current prompt
and renders them into a compact, caveman-compressible "## Prior context" block
that ``models._invoke_attempt`` prepends to planner / implementer prompts
(after the #293 architecture block and the #287 learnings block), so the agent
starts with the project's hard-won memory instead of re-discovering it.

Design notes (mirrors ``architecture_context`` and ``learnings_context``):
- Pure reads from ``memory_index``; this module never emits events. The caller
  emits the ``memory.consulted`` audit event when a block applies.
- Budget-bounded via ``budgets.estimate_tokens`` so prompt-context injections
  share one heuristic with model accounting.
- Best-effort: any failure here must be swallowed by the caller; ``None`` means
  "nothing relevant to inject", not an error. Memory is advisory; a stale or
  wrong recall must never break invocation.
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from .budgets import estimate_tokens

DEFAULT_BUDGET_TOKENS = 500
DEFAULT_TOP_K = 5

# Planner + implementer get the prior-context block up front. The reviewer /
# triager read the store at their own decision sites (same gating as
# ``learnings_context._INJECT_ROLES``).
_INJECT_ROLES = ("planner", "implementer")


def _render_block(results: List[dict]) -> Optional[str]:
    if not results:
        return None
    lines: List[str] = [
        "## Prior context (this project's memory — apply, do not echo)",
        "",
        "Most relevant snippets recalled from prior sessions on this project:",
        "",
    ]
    for r in results:
        source = r.get("source", "")
        source_id = r.get("source_id", "")
        snippet = (r.get("snippet") or r.get("title") or "").strip()
        snippet = " ".join(snippet.split())  # collapse whitespace for compactness
        lines.append(f"- [{source}:{source_id}] {snippet}")
    return "\n".join(lines) + "\n"


def _bound_to_budget(block: str, budget_tokens: int) -> str:
    """Trim the block to the token budget on a line boundary if needed."""
    if budget_tokens <= 0 or estimate_tokens(block) <= budget_tokens:
        return block
    # estimate_tokens ~= chars/4; keep headroom for the wrapper newline.
    max_chars = max(0, budget_tokens * 4)
    if len(block) <= max_chars:
        return block
    trimmed = block[:max_chars].rsplit("\n", 1)[0].rstrip()
    return trimmed + "\n  …\n"


def memory_context_block(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    role: str,
    text: Optional[str] = None,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    top_k: int = DEFAULT_TOP_K,
) -> Optional[str]:
    """Return the prompt-ready prior-context block, or None when nothing applies.

    ``None`` means "skip injection" — an unsupported role, an empty / missing
    memory index, or no relevant snippets for ``text``. The caller prepends the
    returned string and emits the ``memory.consulted`` audit event. ``text`` is
    the recall query; when absent the block is skipped (nothing to rank on).
    """
    if role not in _INJECT_ROLES:
        return None
    query_text = (text or "").strip()
    if not query_text:
        return None
    from .memory import query_memory

    results = query_memory(
        conn, project_id=project_id, text=query_text, limit=max(1, int(top_k))
    )
    block = _render_block(results)
    if block is None:
        return None
    return _bound_to_budget(block, budget_tokens)
