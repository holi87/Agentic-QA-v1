"""Issue #290 — opt-in bounded task synthesis from real signals.

When the autonomy loop finds an empty queue and ``autonomy.task_synthesis``
is enabled, it asks this module to turn real signals into a small backlog so
an unattended run keeps making progress instead of idling. Advisory
discipline holds: synthesized items enter the normal pipeline as ``queued``
work items; the gates still decide what ships.

v1 source scope (narrow — two in, two deferred):

* **IN — failing tests** from ``reports/last-run.json``. Already classified,
  cleanest signal.
* **IN — coverage_gap learnings** (issue #287) for the active project.
* **OUT — requirements coverage** (needs a req→test map that does not exist).
* **OUT — online crawl→synthesis** (kept distinct from the existing
  ``online_task_synthesis_deferred`` block message).

The coverage architect (#229) already flips the safe subset of
``candidate-tests.json`` to ``generate_now``; we deliberately do NOT synthesize
on top of candidates to avoid double-emitting.

Loop-safety: every synthesized item carries a ``source_signal`` token that is
rendered into its spec markdown. Before synthesizing a signal we skip it when
an **open** work item in the active project already carries the same token
(read each open item's spec and grep for the token). This is O(n) per cycle —
slow but correct; optimize later (a dedicated column or index) if it bites.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .events import EventLog
from .paths import RuntimePaths
from .work_items import (
    create_work_item_from_payload,
    list_work_items,
    read_work_item_spec,
)


# A work item is "closed" (does not block dedup) when it is ``done``. There is
# no ``abandoned`` status in this codebase (see ``VALID_WORK_ITEM_STATUSES``);
# ``failed`` is recoverable and re-queueable, so a still-failed item carrying
# the same signal SHOULD block a respawn — hence only ``done`` is treated as
# closed here.
_CLOSED_STATUSES = {"done"}

_KNOWN_BUG_RE = re.compile(r"@known-bug\b", re.IGNORECASE)
_BUG_FILE_RE = re.compile(r"@bug-\d{3,}\b", re.IGNORECASE)


def synthesize_for_idle(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    *,
    project_id: str,
    max_items: int,
) -> int:
    """Synthesize up to ``max_items`` work items from real signals.

    Returns the count actually created. Best-effort throughout: a single bad
    signal must not abort the batch, and a failed audit must not abort
    synthesis. The cap is shared across BOTH sources combined.
    """
    if max_items <= 0:
        return 0

    # Dedup set: source_signals already carried by an open work item in this
    # project. Built once per cycle.
    open_signals = _open_source_signals(conn, paths, project_id=project_id)
    # Quarantine sets — known-bug handled per-failure via tags; flaky via the
    # learnings store keyed on ``feature_uri::scenario``.
    flaky = _flaky_subjects(conn)

    candidates = _gather_candidates(conn, paths, flaky=flaky)

    created: List[Tuple[str, str]] = []  # (work_item_id, source_signal)
    seen_this_cycle: set[str] = set()
    for candidate in candidates:
        if len(created) >= max_items:
            break
        signal = candidate["source_signal"]
        if signal in open_signals or signal in seen_this_cycle:
            continue
        seen_this_cycle.add(signal)
        try:
            item = create_work_item_from_payload(
                conn,
                paths,
                events,
                candidate["payload"],
                default_sut_root=".",
                project_id=project_id,
            )
        except Exception:
            # One malformed signal must not abort the whole batch.
            continue
        work_item_id = item.get("id") or item.get("work_item_id") or "?"
        created.append((work_item_id, signal))
        # Audit reads from what we just created — never re-scan/re-query.
        _audit_synthesis(
            paths,
            events,
            work_item_id=work_item_id,
            source_signal=signal,
            title=candidate["payload"].get("title", ""),
        )

    return len(created)


def _gather_candidates(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    *,
    flaky: set[str],
) -> List[Dict[str, Any]]:
    """Collect synthesis candidates from the two IN sources.

    Failing tests come first so they are preferred when the cap is tight.
    Each candidate is ``{"source_signal": str, "payload": dict}``.
    """
    out: List[Dict[str, Any]] = []
    out.extend(_failing_test_candidates(paths, flaky=flaky))
    out.extend(_coverage_gap_candidates(conn))
    return out


def _failing_test_candidates(
    paths: RuntimePaths, *, flaky: set[str]
) -> List[Dict[str, Any]]:
    data = _load_last_run(paths)
    if not isinstance(data, dict):
        return []
    failures = data.get("failures") or []
    if not isinstance(failures, list):
        return []
    out: List[Dict[str, Any]] = []
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        tags = failure.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        if any(isinstance(t, str) and _KNOWN_BUG_RE.search(t) for t in tags):
            continue
        if any(isinstance(t, str) and _BUG_FILE_RE.search(t) for t in tags):
            continue
        scenario = failure.get("scenario")
        if not isinstance(scenario, str) or not scenario.strip():
            continue
        feature_uri = failure.get("feature_uri") or ""
        subject = f"{feature_uri}::{scenario}"
        if subject in flaky:
            continue
        signal = f"failing-test::{feature_uri}::{scenario}"
        payload = {
            "title": _truncate(f"Fix failing scenario: {scenario}", 160),
            "priority": "P2",
            "source_signal": signal,
            "business_goal": (
                "Restore a green scenario that is currently failing."
            ),
            "expected_behavior": (
                f"Scenario '{scenario}' from {feature_uri or 'the suite'} passes."
            ),
            "relevant_surfaces": feature_uri or "(unknown feature)",
        }
        out.append({"source_signal": signal, "payload": payload})
    return out


def _coverage_gap_candidates(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    try:
        from .learnings import coverage_gap_subjects

        rows = coverage_gap_subjects(conn)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        subject = row.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            continue
        signal = f"coverage-gap::{subject}"
        payload = {
            "title": _truncate(f"Cover gap: {subject}", 160),
            "priority": "P2",
            "source_signal": signal,
            "business_goal": (
                "Close a recurring coverage gap surfaced by the learnings store."
            ),
            "expected_behavior": (
                f"New tests cover the gap '{subject}'."
            ),
        }
        out.append({"source_signal": signal, "payload": payload})
    return out


def _open_source_signals(
    conn: sqlite3.Connection, paths: RuntimePaths, *, project_id: str
) -> set[str]:
    """Source signals carried by open work items in this project.

    v1 reads each open item's spec markdown and extracts the ``Source signal``
    value. Slow (one file read per item) but correct without a schema change;
    optimize later with a dedicated column if it becomes a hotspot.
    """
    signals: set[str] = set()
    try:
        items = list_work_items(conn, project_id=project_id)
    except Exception:
        return signals
    for item in items:
        status = (item.get("status") or "").lower()
        if status in _CLOSED_STATUSES:
            continue
        try:
            spec = read_work_item_spec(paths, item)
        except Exception:
            continue
        token = _extract_source_signal(spec)
        if token:
            signals.add(token)
    return signals


_SOURCE_SIGNAL_LINE = re.compile(
    r"(?im)^\s*Source signal:\s*(?P<value>\S.*?)\s*$"
)


def _extract_source_signal(spec: str) -> Optional[str]:
    match = _SOURCE_SIGNAL_LINE.search(spec)
    if match is None:
        return None
    value = match.group("value").strip()
    if not value or value == "(none)":
        return None
    return value


def _flaky_subjects(conn: sqlite3.Connection) -> set[str]:
    try:
        from .learnings import flaky_subjects

        return set(flaky_subjects(conn))
    except Exception:
        return set()


def _audit_synthesis(
    paths: RuntimePaths,
    events: EventLog,
    *,
    work_item_id: str,
    source_signal: str,
    title: str,
) -> None:
    """Emit the synthesis audit trail. Best-effort: a failure here must not
    abort synthesis (mirrors ``analysis._apply_coverage_architect``)."""
    try:
        events.write(
            "work_item.synthesized",
            actor="planner-autopilot",
            payload={
                "work_item_id": work_item_id,
                "source_signal": source_signal,
                "title": title,
            },
        )
    except Exception:
        pass
    try:
        from .decisions import record_autopilot_decision
        from .orchestrator import CURRENT_PHASE_ID

        record_autopilot_decision(
            paths,
            phase_id=CURRENT_PHASE_ID,
            topic=f"synthesized work item {work_item_id}",
            actor="planner-autopilot",
            rationale=f"task synthesis from signal: {source_signal}",
            consequences="queued a P2 work item for the normal pipeline",
        )
    except Exception:
        pass


def _load_last_run(paths: RuntimePaths) -> Optional[dict]:
    target = paths.repo_root / "reports" / "last-run.json"
    if not target.is_file():
        return None
    try:
        import json

        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
