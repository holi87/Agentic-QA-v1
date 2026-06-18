"""Tests for honest step outcome reporting in autonomy loop (issue #134)."""
from __future__ import annotations

from agentic_os.autonomy import (
    _SessionState,
    _autonomy_step,
    _interpret_step_result,
)


# ---------- _interpret_step_result ----------------------------------------


def test_interpret_run_tests_failure_via_exit_code():
    ok, detail = _interpret_step_result(
        {"ok": False, "exit_code": 1, "failure_kind": "tests_failed", "manifest_path": "x"}
    )
    assert ok is False
    assert "exit_code=1" in detail
    assert "ok=False" in detail
    assert "failure_kind=tests_failed" in detail


def test_interpret_run_tests_success():
    ok, detail = _interpret_step_result(
        {"ok": True, "exit_code": 0, "failure_kind": None, "manifest_path": "x"}
    )
    assert ok is True
    assert "exit_code=0" in detail


def test_interpret_status_blocked_marks_failure():
    ok, detail = _interpret_step_result({"status": "blocked", "next_action": "approve candidate"})
    assert ok is False
    assert "status=blocked" in detail
    assert "next=approve candidate" in detail


def test_interpret_error_field_marks_failure():
    ok, detail = _interpret_step_result(
        {"status": "planned", "error": "missing OpenAPI spec"}
    )
    assert ok is False
    assert "error=missing OpenAPI spec" in detail


def test_interpret_status_analyzing_is_success():
    ok, detail = _interpret_step_result(
        {"status": "analyzing", "work_item_id": "wi_1", "artifacts": []}
    )
    assert ok is True
    assert "status=analyzing" in detail


def test_interpret_non_dict_result_defaults_ok():
    ok, detail = _interpret_step_result(None)
    assert ok is True
    assert detail == "ok"

    ok2, detail2 = _interpret_step_result("done")
    assert ok2 is True
    assert detail2 == "done"


def test_interpret_non_zero_exit_without_ok_field_marks_failure():
    ok, detail = _interpret_step_result({"exit_code": 2, "status": "ran"})
    assert ok is False
    assert "exit_code=2" in detail


def test_interpret_string_exit_code_handled():
    # Defensive: callers might pass exit_code as string. Non-numeric ignored.
    ok, _ = _interpret_step_result({"exit_code": "abc"})
    assert ok is True


# ---------- _autonomy_step integration ------------------------------------


def _make_session() -> _SessionState:
    return _SessionState(
        session_id="s",
        started_at="2026-01-01T00:00:00+00:00",
        expected_finish_at="2026-01-01T00:01:00+00:00",
        max_minutes=1,
    )


def test_autonomy_step_records_failure_when_run_tests_exit_nonzero():
    session = _make_session()

    def fake_run_tests(conn, paths, events, *, work_item_id):
        return {"ok": False, "exit_code": 1, "failure_kind": "tests_failed", "manifest_path": "x"}

    _autonomy_step(session, None, None, None, "wi_1", "run-tests", fake_run_tests)

    assert len(session.events_log) == 1
    evt = session.events_log[0]
    assert evt["step"] == "run-tests:wi_1"
    assert evt["ok"] is False
    assert "exit_code=1" in evt["detail"]


def test_autonomy_step_records_success_when_step_ok():
    session = _make_session()

    def fake_ok(conn, paths, events, *, work_item_id):
        return {"status": "planned", "next_action": None}

    _autonomy_step(session, None, None, None, "wi_2", "plan", fake_ok)

    evt = session.events_log[0]
    assert evt["ok"] is True
    assert "status=planned" in evt["detail"]


def test_autonomy_step_records_failure_on_exception():
    session = _make_session()

    def fake_raises(conn, paths, events, *, work_item_id):
        raise RuntimeError("kaboom")

    _autonomy_step(session, None, None, None, "wi_3", "analyze", fake_raises)

    evt = session.events_log[0]
    assert evt["ok"] is False
    assert "kaboom" in evt["detail"]


def test_autonomy_step_detail_truncated_to_500_chars():
    session = _make_session()

    def fake_long(conn, paths, events, *, work_item_id):
        return {"error": "x" * 2000}

    _autonomy_step(session, None, None, None, "wi_4", "plan", fake_long)

    evt = session.events_log[0]
    assert evt["ok"] is False
    assert len(evt["detail"]) <= 500
