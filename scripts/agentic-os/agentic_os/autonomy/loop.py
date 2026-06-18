"""Main autonomy worker loop + per-step result interpretation (issue #292)."""
from __future__ import annotations
from agentic_os import autonomy as _aut  # late-binding access for monkey-patchable tuning consts (#292)

import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

from .. import task_synthesis
from ..events import EventLog, event_log_for_paths
from ..paths import RuntimePaths
from ..runtime.tuning import (
    EVENTS_LOG_RING_SIZE as _EVENTS_LOG_RING_SIZE,
    RECORD_DETAIL_MAX_CHARS as _RECORD_DETAIL_MAX_CHARS,
    SHUTDOWN_GRACE_SECONDS as _SHUTDOWN_GRACE_SECONDS,
)
from ..storage import init_db





def _run_loop(
    paths: RuntimePaths,
    session: _SessionState,
    stop_event: threading.Event,
    pause_event: Optional[threading.Event] = None,
) -> None:
    """Worker — drives the pipeline until time runs out or stop is requested."""
    if pause_event is None:
        pause_event = threading.Event()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=session.max_minutes)
    try:
        from ..analysis import analyze_work_item
        from ..patch_builder import implement_tests_for_work_item
        from ..test_planning import plan_work_item
        from ..work_items import list_work_items
    except ImportError as exc:
        with _MANAGER.lock:
            session.status = "failed"
            session.finished_at = _now_iso()
            session.error = f"import failure: {exc}"
        return

    try:
        conn = init_db(paths.db)
    except (sqlite3.Error, OSError) as exc:
        with _MANAGER.lock:
            session.status = "failed"
            session.finished_at = _now_iso()
            session.error = f"db open failed: {exc}"
        return
    events = event_log_for_paths(conn, paths)

    # Issue #269 — durable session index row for the /sessions history view.
    try:
        from ..sessions import record_session_start
        record_session_start(
            conn,
            session_id=session.session_id,
            started_at=session.started_at,
            mode="loop",
            max_minutes=session.max_minutes,
            primary_actor="autonomy",
        )
    except Exception:  # pragma: no cover - persistence must not break the loop
        pass

    _record(session, "boot", True, f"max_minutes={session.max_minutes}")
    if session.preflight and not session.preflight.get("ok"):
        failing = [
            c["id"] for c in session.preflight.get("checks", [])
            if c.get("status") == "fail"
        ]
        # Only a hard `fail` is worth a boot-level failure record. A warn-only
        # preflight (e.g. online-only with no test_runner, #317) is not blocking
        # and must not surface as a misleading "failing: unknown".
        if failing:
            _record(
                session,
                "preflight",
                False,
                f"failing: {','.join(failing)} — see /api/autonomy/preflight for actions",
            )

    consecutive_stack_unknown = 0

    try:
        while not stop_event.is_set():
            _wait_if_paused(session, stop_event, pause_event)
            if stop_event.is_set():
                break
            if datetime.now(timezone.utc) >= deadline:
                _record(session, "deadline", True, "max time reached")
                break
            try:
                items = list_work_items(conn) or []
            except sqlite3.Error as exc:
                _record(session, "list_work_items", False, str(exc))
                items = []
            pending = _select_pending(items)
            # Issue #274 — reorder the pending list by the configured queue
            # policy. Default is HYBRID, which degrades to the canonical FIFO
            # order (created_at ASC, id ASC) when no priorities, dependency
            # edges or budget signal exist, so default behaviour is preserved.
            # Best-effort: a config/db hiccup must never stall the loop, so we
            # fall back to the un-reordered list on any failure.
            if pending:
                try:
                    from .. import queue as _queue

                    pending = _queue.order_pending(
                        conn, pending, policy=_resolve_queue_policy(paths)
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    _record(session, "queue_policy", False, str(exc))
            if not pending:
                # Issue #290 — opt-in bounded synthesis BEFORE exploratory/idle.
                # Best-effort: any failure (config, project resolve, the synth
                # itself) falls through to the unchanged exploratory path.
                synth_cfg = _load_cfg_best_effort(paths)
                if synth_cfg is not None and _task_synthesis_enabled(synth_cfg):
                    synthesized = 0
                    try:
                        active_project = _resolve_active_project_best_effort(
                            conn, synth_cfg
                        )
                        synthesized = task_synthesis.synthesize_for_idle(
                            conn,
                            paths,
                            events,
                            project_id=active_project,
                            max_items=_task_synthesis_cap(synth_cfg),
                        )
                    except Exception as exc:
                        _record(session, "task_synthesis", False, str(exc))
                        synthesized = 0
                    if synthesized > 0:
                        _record(
                            session, "task_synthesis", True, f"created {synthesized}"
                        )
                        # Next iteration picks them up; skip exploratory/idle.
                        if stop_event.wait(timeout=0):
                            break
                        continue
                with _MANAGER.lock:
                    session.awaiting_task = True
                paused = consecutive_stack_unknown >= _aut._EXPLORATORY_FAILURE_THRESHOLD
                with _MANAGER.lock:
                    paused = paused or bool(session.paused_reason)
                if not paused:
                    _record(
                        session,
                        "idle:awaiting-task",
                        True,
                        "no pending work items — running exploratory discovery while awaiting a task assignment",
                    )
                # Always probe — when paused the slower poll keeps it
                # cheap, and a successful probe is the only way an
                # operator's config fix can lift the pause without
                # queuing a task.
                try:
                    from .exploratory import _exploratory_pass, _maybe_exploratory_baseline
                    ran_baseline = _maybe_exploratory_baseline(session, conn, paths, events)
                    discovery_signal = None if ran_baseline else _exploratory_pass(session, paths)
                except Exception as exc:  # intentionally broad: exploratory probe spans subprocess/fs/crawler — a failure must record and keep the daemon loop alive, not kill it
                    _record(session, "exploratory", False, str(exc))
                    discovery_signal = None
                if discovery_signal == "stack_unknown":
                    consecutive_stack_unknown += 1
                    if consecutive_stack_unknown == _aut._EXPLORATORY_FAILURE_THRESHOLD:
                        reason = (
                            f"stack=unknown for {_aut._EXPLORATORY_FAILURE_THRESHOLD} consecutive passes — "
                            "pausing exploratory discovery. Fix the SUT config (see /api/autonomy/preflight) "
                            "or queue a task to resume."
                        )
                        with _MANAGER.lock:
                            session.paused_reason = reason
                        _record(session, "idle:paused", True, reason)
                elif discovery_signal == "online_task_synthesis_deferred":
                    consecutive_stack_unknown = 0
                    reason = (
                        "online web URL was crawled from sut.web.url, but empty-queue "
                        "task synthesis is deferred to issue #290; enable "
                        "autonomy.exploratory_baseline or queue a task to direct the run."
                    )
                    _record_block_reason(session, reason)
                elif discovery_signal == "online_crawl_failed":
                    consecutive_stack_unknown = 0
                    reason = (
                        "online web URL discovery failed; fix sut.web.url/network access "
                        "or queue a task with explicit scope."
                    )
                    _record_block_reason(session, reason)
                else:
                    should_resume = consecutive_stack_unknown >= _aut._EXPLORATORY_FAILURE_THRESHOLD
                    with _MANAGER.lock:
                        should_resume = should_resume or bool(session.paused_reason)
                    if should_resume:
                        _record(
                            session,
                            "idle:resumed",
                            True,
                            "discovery healthy — resuming standard polling cadence",
                        )
                        with _MANAGER.lock:
                            session.paused_reason = None
                    consecutive_stack_unknown = 0
                with _MANAGER.lock:
                    blocked_or_paused = bool(session.paused_reason)
                poll = _aut._PAUSED_POLL_SECONDS if (
                    consecutive_stack_unknown >= _aut._EXPLORATORY_FAILURE_THRESHOLD or blocked_or_paused
                ) else _aut._ACTIVE_POLL_SECONDS
                if stop_event.wait(timeout=poll):
                    break
                continue

            with _MANAGER.lock:
                session.awaiting_task = False
                session.paused_reason = None
            consecutive_stack_unknown = 0
            for wi in pending:
                _wait_if_paused(session, stop_event, pause_event)
                if stop_event.is_set() or datetime.now(timezone.utc) >= deadline:
                    break
                work_id = wi.get("id")
                if not work_id:
                    continue
                # Issue #290 — checkpoint+resume. Each early phase is gated on
                # its proof artifact: a phase that already produced its
                # artifact is skipped (so a resume re-runs only the
                # missing/failed phase). The step still breaks the sequence on
                # the first failure so downstream phases do not run on top of a
                # failed upstream one; the item stays pending and the next
                # iteration resumes the failed phase.
                if not _phase_done(conn, work_id, "analyze"):
                    if not _autonomy_step(
                        session, conn, paths, events, work_id, "analyze", analyze_work_item
                    ):
                        break
                if stop_event.is_set() or datetime.now(timezone.utc) >= deadline:
                    break
                if not _phase_done(conn, work_id, "plan"):
                    if not _autonomy_step(
                        session, conn, paths, events, work_id, "plan", plan_work_item
                    ):
                        break
                if stop_event.is_set() or datetime.now(timezone.utc) >= deadline:
                    break
                if not _phase_done(conn, work_id, "implement"):
                    if not _autonomy_step(
                        session, conn, paths, events, work_id, "implement", implement_tests_for_work_item
                    ):
                        break
                if stop_event.is_set() or datetime.now(timezone.utc) >= deadline:
                    break
                # Issue #81 — extend the autonomous loop beyond
                # implement so the feature actually delivers a QA
                # outcome. Steps after implement are gated behind
                # `_should_continue_to_review()` which inspects the
                # work item status: a `blocked` status (no approved
                # candidate, plan validation failure, etc.) yields a
                # clear `awaiting_operator_decision` signal in the
                # session events instead of looping forever.
                if not _should_continue_to_review(conn, work_id):
                    _record(
                        session,
                        f"{work_id}:awaiting_operator_decision",
                        False,
                        "no `generate_now` candidate; autonomy halted at implement step",
                    )
                    continue
                _autonomy_step(
                    session,
                    conn,
                    paths,
                    events,
                    work_id,
                    "review-gate",
                    _aut._autonomy_review_then_apply,
                )
                if stop_event.is_set() or datetime.now(timezone.utc) >= deadline:
                    break
                _autonomy_step(
                    session,
                    conn,
                    paths,
                    events,
                    work_id,
                    "run-tests",
                    _aut._autonomy_run_tests,
                )
                if stop_event.is_set() or datetime.now(timezone.utc) >= deadline:
                    break
                _autonomy_step(
                    session,
                    conn,
                    paths,
                    events,
                    work_id,
                    "final-gate",
                    _aut._autonomy_final_gate,
                )
            # Loop continues — re-check items in case new ones were queued.
    finally:
        with _MANAGER.lock:
            if session.status in ("running", "paused"):
                session.status = "stopped" if stop_event.is_set() else "finished"
            session.finished_at = _now_iso()
        # Issue #269 — finalise the durable session row. Counts come from the
        # session's running counters (issue #265), not a re-scan of the
        # bounded events_log, so long sessions are not undercounted when the
        # ring buffer drops old entries.
        try:
            from ..sessions import finalize_session
            finalize_session(
                conn,
                session_id=session.session_id,
                status=session.status,
                finished_at=session.finished_at,
                work_items_processed=len(session.processed_work_items),
                blocks=session.block_count,
                failures=session.failure_count,
            )
        except Exception:  # pragma: no cover - persistence must not break shutdown
            pass
        # Issue #272 — render the PR-ready handoff doc before the completion
        # event so its link can ride along in the notification payload. The
        # in-memory processed list is authoritative; CLI re-renders later
        # derive from the time window.
        summary_path: Optional[str] = None
        try:
            from ..summaries import summary_relpath, write_session_summary
            written = write_session_summary(
                conn,
                session.session_id,
                reports_dir=paths.repo_root / "reports",
                processed_work_items=session.processed_work_items,
            )
            if written is not None:
                summary_path = summary_relpath(session.session_id)
        except Exception:  # pragma: no cover - summary must not break shutdown
            pass
        # Issue #268 — emit a real completion event so the notification
        # classifier's `session_completed` path actually fires.
        try:
            events.write(
                "autonomy.completed",
                payload={
                    "session_id": session.session_id,
                    "status": session.status,
                    "finished_at": session.finished_at,
                    "summary_path": summary_path,
                },
            )
        except Exception:  # pragma: no cover - best-effort notification hook
            pass
        try:
            conn.close()
        except Exception:
            pass
_PHASE_ARTIFACT_KIND: Dict[str, str] = {
    "analyze": "analysis",
    "plan": "test_plan",
    "implement": "patch",
}

def _phase_done(conn: sqlite3.Connection, work_item_id: str, phase: str) -> bool:
    """True when ``phase`` already produced its proof artifact for the item.

    Best-effort: any failure (unknown phase, db hiccup) returns ``False`` so a
    check failure merely re-runs the phase rather than skipping it. Maps the
    phase to its required artifact kind via :func:`list_work_item_artifacts`.
    """
    kind = _PHASE_ARTIFACT_KIND.get(phase)
    if kind is None:
        return False
    try:
        from ..work_items import list_work_item_artifacts

        return any(
            a["kind"] == kind for a in list_work_item_artifacts(conn, work_item_id)
        )
    except Exception:  # pragma: no cover - defensive: a check failure re-runs the phase
        return False

def _should_continue_to_review(conn: sqlite3.Connection, work_item_id: str) -> bool:
    """Issue #81 — only continue to review/apply when implement actually
    produced an executable patch. A work item that ended up `blocked`
    (no approved candidate, plan validator rejected metadata, etc.) is
    a checkpoint — autonomy halts and surfaces the gap.
    """
    from ..work_items import get_work_item, list_work_item_artifacts

    wi = get_work_item(conn, work_item_id)
    if wi is None or wi.get("status") == "blocked":
        return False
    return any(a["kind"] == "patch" for a in list_work_item_artifacts(conn, work_item_id))

_FAILURE_STATUSES = frozenset({"blocked", "error", "failed"})

def _interpret_step_result(result: Any) -> tuple[bool, str]:
    """Honest read of a pipeline call's outcome.

    Returns ``(ok, detail)``. A dict result is considered a failure when
    any of these hold: ``ok`` is falsy, ``exit_code`` is non-zero,
    ``error`` is truthy, or ``status`` is in ``_FAILURE_STATUSES``.
    Non-dict results default to ``ok=True``.
    """
    if not isinstance(result, dict):
        return True, str(result if result is not None else "ok")

    ok = True
    parts: list[str] = []

    if "ok" in result:
        ok = bool(result["ok"])
        parts.append(f"ok={result['ok']}")

    exit_code = result.get("exit_code")
    if exit_code is not None:
        try:
            if int(exit_code) != 0:
                ok = False
        except (TypeError, ValueError):
            pass
        parts.append(f"exit_code={exit_code}")

    status = result.get("status")
    if status is not None:
        parts.append(f"status={status}")
        if str(status) in _FAILURE_STATUSES:
            ok = False

    error = result.get("error")
    if error:
        ok = False
        parts.append(f"error={error}")

    failure_kind = result.get("failure_kind")
    if failure_kind:
        parts.append(f"failure_kind={failure_kind}")

    next_action = result.get("next_action")
    if next_action:
        parts.append(f"next={next_action}")

    if not parts:
        parts.append("ok")
    return ok, " ".join(parts)

def _autonomy_step(
    session: _SessionState,
    conn: Any,
    paths: RuntimePaths,
    events: EventLog,
    work_id: str,
    step: str,
    func: Any,
) -> bool:
    """Run a single pipeline call, record honest outcome, swallow exceptions.

    Earlier versions recorded every call as a success, masking failing
    ``run-tests`` and ``final-gate`` runs on the dashboard (issue #134).
    The outcome is now derived from the pipeline result via
    :func:`_interpret_step_result`.

    Issue #290 (checkpoint+resume) — returns ``ok`` (bool) so the per-item
    loop can break the phase sequence on the first failure instead of running
    downstream phases on top of a failed upstream one. A caught exception is
    recorded and reported as ``False``.

    Issue #308 — pipeline builders that accept a ``session_id`` kwarg
    receive ``session.session_id`` so model invocations record a row keyed
    to the active session (and `budget_status` reflects real cost).
    Builders without the kwarg are called with the historic signature so
    legacy non-pipeline callees stay unaffected.
    """
    import inspect

    extras: Dict[str, Any] = {}
    try:
        sig = inspect.signature(func)
        if "session_id" in sig.parameters:
            extras["session_id"] = session.session_id
    except (TypeError, ValueError):
        # `func` may be a callable without an introspectable signature
        # (e.g. a builtin-style wrapper). Fall back to the legacy call.
        extras = {}
    try:
        result = func(conn, paths, events, work_item_id=work_id, **extras)
    except Exception as exc:  # intentionally broad: a single pipeline-step failure must record an honest outcome and let the session continue, never kill the worker (see docstring / issue #134)
        _record(session, f"{step}:{work_id}", False, str(exc))
        return False

    ok, detail = _interpret_step_result(result)
    _record(session, f"{step}:{work_id}", ok, detail)
    return ok

# `_record` / `_record_block_reason` moved to session_state.py (issue #292).
from .session_state import _record, _record_block_reason  # noqa: F401
# dispatch funcs accessed via `_aut.X` so tests can monkey-patch them on the package (#292).
# loop→exploratory is a cycle (exploratory→session_state); lazy import in _run_loop (#292).
from .queue import _load_cfg_best_effort, _resolve_active_project_best_effort, _resolve_queue_policy, _select_pending, _task_synthesis_cap, _task_synthesis_enabled, _wait_if_paused
from .session_state import _MANAGER, _SessionState, _now_iso
