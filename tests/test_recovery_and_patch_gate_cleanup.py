"""Recovery workflow and final-gate patch cleanup behavior."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentic_os.errors import UsageError
from agentic_os.events import EventLog
from agentic_os.gates import (
    GateResult,
    describe_blocking_patches,
    find_patch_gate_violations,
    write_gate_result,
)
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.work_items import (
    create_work_item_from_payload,
    register_work_item_artifact,
)
from agentic_os.workflows import abandon_patch, run_recovery


def _runtime(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    return conn, paths, events, orch


def _make_work_item(conn, paths, events) -> str:
    detail = create_work_item_from_payload(
        conn,
        paths,
        events,
        {
            "title": "STEP2 phase01 fixture",
            "business_goal": "test recovery and abandon",
            "expected_behavior": "n/a",
        },
        default_sut_root=".",
    )
    return detail["work_item"]["id"]


def _register_patch(conn, paths, events, work_item_id: str, *, name: str = "p.patch") -> Path:
    patch = paths.repo_root / name
    patch.write_text("diff --git a/x b/x\n", encoding="utf-8")
    register_work_item_artifact(
        conn,
        paths,
        events,
        work_item_id=work_item_id,
        kind="patch",
        path=name,
    )
    return patch


def test_run_recovery_returns_workflow_result(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        result = run_recovery(orch, paths, events)
        assert result.ok is True
        assert result.exit_code == 0
        assert result.task_id
        assert result.run_id
        assert Path(paths.repo_root / result.manifest_path).is_file()
    finally:
        conn.close()


def test_run_recovery_does_not_leave_queued_task(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        run_recovery(orch, paths, events)
        queued = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE kind='recovery' AND status='queued';"
        ).fetchone()
        assert queued["n"] == 0
    finally:
        conn.close()


def test_run_recovery_writes_manifest_with_scan(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        result = run_recovery(orch, paths, events)
        manifest_text = (paths.repo_root / result.manifest_path).read_text(encoding="utf-8")
        assert "recovery" in manifest_text
        assert "db_integrity" in manifest_text
    finally:
        conn.close()


def test_run_recovery_no_orphaned_fk(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        run_recovery(orch, paths, events)
        rows = conn.execute("PRAGMA foreign_key_check;").fetchall()
        assert rows == []
    finally:
        conn.close()


def test_final_gate_blocks_unapproved_patch(tmp_path: Path) -> None:
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        work_item_id = _make_work_item(conn, paths, events)
        _register_patch(conn, paths, events, work_item_id)

        violations = find_patch_gate_violations(paths, conn=conn)
        assert violations, "unapproved patch must block final gate"
        assert "APPROVE gate" in violations[0].message

        states = describe_blocking_patches(paths, conn=conn)
        assert len(states) == 1
        assert states[0]["state"] == "waiting"
        assert states[0]["blocking"] is True
    finally:
        conn.close()


def test_final_gate_passes_approved_patch(tmp_path: Path) -> None:
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        work_item_id = _make_work_item(conn, paths, events)
        patch_file = _register_patch(conn, paths, events, work_item_id)
        approve = GateResult(
            verdict="APPROVE",
            reason="static_checks_passed",
            findings=[],
        )
        gate_path = write_gate_result(paths, approve, name="api")
        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_item_id,
            kind="gate",
            path=str(gate_path.relative_to(paths.repo_root)),
        )
        # Issue #87 — APPROVE alone is no longer enough; an `apply`
        # artifact must record that the patch reached the working tree.
        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_item_id,
            kind="apply",
            path=str(patch_file.relative_to(paths.repo_root)),
        )
        assert find_patch_gate_violations(paths, conn=conn) == []
        states = describe_blocking_patches(paths, conn=conn)
        assert states[0]["state"] == "approved"
        assert states[0]["blocking"] is False
    finally:
        conn.close()


def test_final_gate_blocks_approved_patch_without_apply(tmp_path: Path) -> None:
    """Issue #87 — APPROVE gate without `apply` artifact must still block."""
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        work_item_id = _make_work_item(conn, paths, events)
        _register_patch(conn, paths, events, work_item_id)
        approve = GateResult(
            verdict="APPROVE",
            reason="static_checks_passed",
            findings=[],
        )
        gate_path = write_gate_result(paths, approve, name="api")
        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_item_id,
            kind="gate",
            path=str(gate_path.relative_to(paths.repo_root)),
        )
        violations = find_patch_gate_violations(paths, conn=conn)
        assert violations, "approved-but-unapplied patch must block final gate"
        assert "no `apply` artifact" in violations[0].message
        states = describe_blocking_patches(paths, conn=conn)
        assert states[0]["state"] == "approved_pending_apply"
        assert states[0]["blocking"] is True
    finally:
        conn.close()


def test_abandon_patch_unblocks_final_gate_and_keeps_history(tmp_path: Path) -> None:
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        work_item_id = _make_work_item(conn, paths, events)
        patch_file = _register_patch(conn, paths, events, work_item_id)

        # Pre-condition: patch is blocking.
        assert find_patch_gate_violations(paths, conn=conn)

        result = abandon_patch(
            paths,
            events,
            task_id=work_item_id,
            patch_path=str(patch_file.relative_to(paths.repo_root)),
            reason="known infra flake; tracked elsewhere",
        )
        assert result["decision_id"]
        assert result["task_id"] == work_item_id

        # The patch row stays — abandon must not erase history.
        patch_count = conn.execute(
            "SELECT COUNT(*) AS n FROM work_item_artifacts WHERE work_item_id=? AND kind='patch';",
            (work_item_id,),
        ).fetchone()["n"]
        assert patch_count == 1

        # Decision row exists.
        decision = conn.execute(
            "SELECT topic, decided_by, rationale FROM decisions WHERE id=?;",
            (result["decision_id"],),
        ).fetchone()
        assert decision["decided_by"] == "operator"
        assert decision["topic"].startswith("patch_abandoned:")

        # Final gate accepts abandoned patch.
        assert find_patch_gate_violations(paths, conn=conn) == []

        states = describe_blocking_patches(paths, conn=conn)
        assert states[0]["state"] == "abandoned"
        assert states[0]["blocking"] is False
    finally:
        conn.close()


def test_abandon_patch_requires_reason(tmp_path: Path) -> None:
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        work_item_id = _make_work_item(conn, paths, events)
        patch_file = _register_patch(conn, paths, events, work_item_id)
        with pytest.raises(UsageError):
            abandon_patch(
                paths,
                events,
                task_id=work_item_id,
                patch_path=str(patch_file.relative_to(paths.repo_root)),
                reason="   ",
            )
    finally:
        conn.close()


def test_abandon_patch_rejects_unknown_patch(tmp_path: Path) -> None:
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        work_item_id = _make_work_item(conn, paths, events)
        with pytest.raises(UsageError):
            abandon_patch(
                paths,
                events,
                task_id=work_item_id,
                patch_path="ghost.patch",
                reason="should fail",
            )
    finally:
        conn.close()
