"""Coverage architect pass — autopilot rules, recurring-gap recording.

Split from analysis.py (issue #292).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import load_or_default
from ..paths import RuntimePaths


def _apply_coverage_architect(
    paths: RuntimePaths,
    candidates_payload: Dict[str, Any],
    summary: Dict[str, int],
    *,
    conn: Any = None,
) -> None:
    if not _coverage_architect_enabled(paths):
        return
    items = candidates_payload.get("items") or []
    if not isinstance(items, list):
        return
    flipped = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("decision") != "needs_operator_decision":
            continue
        rule = _autopilot_decision_rule(item)
        if rule is None:
            continue
        item["decision"] = "generate_now"
        item["actor"] = "planner-autopilot"
        notes = list(item.get("notes") or [])
        notes.append(f"Auto-promoted by planner-autopilot ({rule}).")
        item["notes"] = notes
        flipped += 1
        # Issue #247 — record the autonomous decision so the Verifications
        # view can surface the planner-autopilot trail. Best-effort.
        try:
            from ..decisions import record_autopilot_decision
            from ..orchestrator import CURRENT_PHASE_ID

            cand_id = item.get("candidate_id") or item.get("id") or "?"
            record_autopilot_decision(
                paths,
                phase_id=CURRENT_PHASE_ID,
                topic=f"candidate {cand_id}: generate_now",
                actor="planner-autopilot",
                rationale=f"coverage architect rule: {rule}",
                consequences="candidate promoted from needs_operator_decision to generate_now",
            )
        except Exception:
            pass
    if flipped:
        summary["planner_autopilot_flipped"] = flipped

    # Issue #287 — coverage_gap producer runs after promotion so it cannot
    # affect any decision. Best-effort and record-only.
    _record_recurring_coverage_gaps(paths, conn, items)


def _coverage_architect_enabled(paths: RuntimePaths) -> bool:
    try:
        from ..config import load_or_default

        cfg = load_or_default(paths.repo_root)
        autonomy = cfg.raw.get("autonomy") or {}
        return bool(autonomy.get("coverage_architect", False))
    except Exception:
        # Config errors must not flip decisions unexpectedly. Treat as off.
        return False


def _autopilot_decision_rule(item: Dict[str, Any]) -> Optional[str]:
    """Return the rule name that authorises auto-promotion, or None."""
    test_type = item.get("test_type")
    if test_type == "api":
        method = (item.get("target_method") or "").upper()
        path = item.get("target_path")
        if method in _AUTONOMOUS_SAFE_API_METHODS and path:
            return f"api-read-only:{method}"
        return None
    if test_type == "ui":
        target_page = item.get("target_page")
        if not target_page:
            return None
        lowered = str(target_page).lower()
        if any(hint in lowered for hint in _UI_FORM_HINTS):
            return None
        return f"ui-navigational:{target_page}"
    return None


def _record_recurring_coverage_gaps(
    paths: RuntimePaths,
    conn: Any,
    items: List[Dict[str, Any]],
) -> None:
    """Record `coverage_gap` learnings for recurring gap categories (issue #287).

    "Recurring" means either (a) two or more candidates of the same gap
    category surface in this run, or (b) a learning for the same SUT+category
    already exists from a prior run. Best-effort and advisory: a failure never
    affects candidate promotion. The conn is optional — without it the producer
    is a no-op so the legacy 3-arg callers keep working unchanged.
    """
    if conn is None:
        return
    try:
        from ..learnings import record_learning

        sut_key = _sut_key(paths)
        counts: Dict[str, int] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            bucket = item.get("bucket")
            category = _GAP_BUCKET_TO_CATEGORY.get(str(bucket))
            if category:
                counts[category] = counts.get(category, 0) + 1
        for category, count in counts.items():
            subject = f"{sut_key}::{category}"
            recurring_in_run = count >= 2
            seen_before = False
            try:
                row = conn.execute(
                    "SELECT 1 FROM learnings WHERE kind='coverage_gap' AND subject=? LIMIT 1;",
                    (subject,),
                ).fetchone()
                seen_before = row is not None
            except Exception:
                seen_before = False
            if recurring_in_run or seen_before:
                record_learning(
                    conn,
                    kind="coverage_gap",
                    subject=subject,
                    payload={
                        "category": category,
                        "sut": sut_key,
                        "candidates_in_run": count,
                    },
                    actor="coverage-architect",
                )
    except Exception:
        pass


def _sut_key(paths: RuntimePaths) -> str:
    """Best-effort SUT identifier for coverage_gap subjects (issue #287)."""
    try:
        from ..config import load_or_default

        cfg = load_or_default(paths.repo_root)
        sut = cfg.raw.get("sut") or {}
        root = str(sut.get("root") or ".").strip()
        return root or "."
    except Exception:
        return "."


_UI_FORM_HINTS = ("/new", "/create", "/edit", "/login", "/signup", "/register")


_AUTONOMOUS_SAFE_API_METHODS = {"GET", "HEAD", "OPTIONS"}


_GAP_BUCKET_TO_CATEGORY = {
    "Accessibility": "accessibility",
    "Security": "security",
    "Not testable now": "not-testable-now",
}
