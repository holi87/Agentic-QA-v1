"""Inject a compressed architecture map into agent prompts (issue #293).

`docs/architecture.md` is the canonical, human-readable map. The block between
the `inject:architecture-summary` markers is the caveman-compressed form that
gets prepended to every model prompt in `models._invoke_attempt`, so planner /
implementer / reviewer / triager share one map instead of re-deriving it.

Design notes:
- The doc is the single source of truth; we extract the block, never duplicate
  it in code.
- Injection is best-effort and budget-bounded; a missing doc or marker returns
  ``None`` and the caller proceeds without the block.
- Token accounting reuses ``budgets.estimate_tokens`` so the prompt-context
  budget shares one heuristic with model accounting (and, later, the #287
  learnings and #289 memory injections).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from .budgets import estimate_tokens

_MARKER_START = "<!-- inject:architecture-summary:start -->"
_MARKER_END = "<!-- inject:architecture-summary:end -->"
_SUMMARY_RE = re.compile(
    re.escape(_MARKER_START) + r"\s*(.*?)\s*" + re.escape(_MARKER_END),
    re.DOTALL,
)
_DOC_REL = Path("docs") / "architecture.md"
DEFAULT_BUDGET_TOKENS = 600

# Cache the extracted summary keyed by (resolved path, mtime) so repeated
# invocations do not re-read the doc, while an edit still invalidates it.
# Bounded so a long-running process (or a test session spawning many tmp
# repos) cannot grow it without limit; a single-repo runtime only ever needs
# one entry, and a re-read on eviction is cheap.
_CACHE_MAX = 16
_cache: Dict[Tuple[str, int], str] = {}


def extract_summary(doc_text: str) -> Optional[str]:
    """Return the text between the inject markers, or None when absent."""
    match = _SUMMARY_RE.search(doc_text)
    if not match:
        return None
    summary = match.group(1).strip()
    return summary or None


def _load_summary(repo_root: Path) -> Optional[str]:
    doc_path = (repo_root / _DOC_REL).resolve()
    try:
        stat = doc_path.stat()
    except OSError:
        return None
    key = (str(doc_path), int(stat.st_mtime_ns))
    cached = _cache.get(key)
    if cached is not None:
        return cached
    try:
        text = doc_path.read_text(encoding="utf-8")
    except OSError:
        return None
    summary = extract_summary(text)
    if summary is not None:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = summary
    return summary


def _bound_to_budget(summary: str, budget_tokens: int) -> str:
    """Trim the summary to the token budget on a word boundary if needed."""
    if budget_tokens <= 0 or estimate_tokens(summary) <= budget_tokens:
        return summary
    # estimate_tokens ~= chars/4; keep a little headroom for the wrapper.
    max_chars = max(0, budget_tokens * 4)
    trimmed = summary[:max_chars].rsplit(" ", 1)[0].rstrip()
    return trimmed + " …"


def architecture_context_block(
    repo_root: Path, *, budget_tokens: int = DEFAULT_BUDGET_TOKENS
) -> Optional[str]:
    """Return the prompt-ready architecture block, or None when unavailable.

    The returned string is a self-contained markdown section the caller can
    prepend to the prompt. ``None`` means the doc/marker is missing — the
    caller must treat that as "skip injection", not an error.
    """
    summary = _load_summary(repo_root)
    if not summary:
        return None
    summary = _bound_to_budget(summary, budget_tokens)
    return (
        "## Architecture context\n\n"
        "Authoritative map of this runtime (read once, apply, do not echo):\n\n"
        f"{summary}\n"
    )
