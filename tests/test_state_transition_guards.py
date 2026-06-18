from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentic_os.config import get_active_config_override, set_active_config_override
from agentic_os.errors import InfraError, UsageError
from agentic_os.events import EventLog
from agentic_os.gates import parse_gate_output, static_review_gate
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import is_full_mode_active, set_full_mode_override
from agentic_os.storage import init_db
from agentic_os.work_items import (
    create_work_item_from_payload,
    delete_work_item,
    update_work_item_status,
    work_item_summary,
)


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    Orchestrator(conn, paths, EventLog(conn, paths)).seed_phases()
    return conn, paths, EventLog(conn, paths)


def test_config_override_is_contextvar_backed(tmp_path: Path) -> None:
    assert get_active_config_override() is None
    override = tmp_path / "agentic-os.yml"
    set_active_config_override(override)
    try:
        assert get_active_config_override() == override
    finally:
        set_active_config_override(None)


def test_full_mode_override_is_contextvar_backed() -> None:
    set_full_mode_override(True)
    try:
        assert is_full_mode_active() is True
    finally:
        set_full_mode_override(False)
    assert is_full_mode_active() is False


def test_work_item_ids_include_ulid_entropy(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        first = create_work_item_from_payload(conn, paths, events, {"title": "same title"})["work_item"]["id"]
        second = create_work_item_from_payload(conn, paths, events, {"title": "same title"})["work_item"]["id"]
        assert first != second
        assert first.startswith("TASK-")
        assert len(first.split("-")) >= 5
    finally:
        conn.close()


def test_invalid_work_item_transition_is_rejected(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        wid = create_work_item_from_payload(conn, paths, events, {"title": "transition"})["work_item"]["id"]
        update_work_item_status(conn, events, work_item_id=wid, status="failed")
        with pytest.raises(UsageError, match="invalid work item status transition"):
            update_work_item_status(conn, events, work_item_id=wid, status="done")
    finally:
        conn.close()


def test_work_item_summary_tracks_unknown_status_bucket(tmp_path: Path) -> None:
    conn, _paths, _events = _runtime(tmp_path)
    try:
        conn.execute("PRAGMA ignore_check_constraints=ON;")
        conn.execute(
            """
            INSERT INTO work_items(id, title, status, spec_path, sut_root, priority, created_at, updated_at)
            VALUES ('TASK-20260101-000000-unknown', 'unknown', 'mystery', 'x.md', '.', 'P2', 'now', 'now');
            """
        )
        summary = work_item_summary(conn)
        assert summary["unknown"] == 1
        assert summary["total"] == 1
    finally:
        conn.close()


def test_delete_work_item_keeps_db_row_when_runtime_delete_fails(tmp_path: Path, monkeypatch) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        wid = create_work_item_from_payload(conn, paths, events, {"title": "delete failure"})["work_item"]["id"]
        (paths.patches_dir / wid).mkdir(parents=True)

        def fail_remove(_path: Path) -> None:
            raise OSError("locked")

        monkeypatch.setattr("agentic_os.work_items._remove_tree", fail_remove)
        with pytest.raises(InfraError):
            delete_work_item(conn, paths, events, work_item_id=wid)
        row = conn.execute("SELECT id FROM work_items WHERE id=?;", (wid,)).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_static_gate_rejects_fake_operator_decision_comment() -> None:
    diff = (
        "diff --git a/tests/example.spec.ts b/tests/example.spec.ts\n"
        "+++ b/tests/example.spec.ts\n"
        "@@ -0,0 +1 @@\n"
        "+test.skip('broken'); # operator-decision: yes\n"
    )

    gate = static_review_gate(diff, scope="api")

    assert gate.verdict == "REJECT"
    assert gate.reason == "skip_without_decision"


def test_parse_gate_output_accepts_duplicate_ready_terminator() -> None:
    gate = parse_gate_output(
        "verdict: APPROVE\n"
        "reason: ok\n"
        "\n"
        "findings:\n"
        "- OK:1 - no blocking findings\n"
        "READY\n"
        "READY\n"
    )

    assert gate.verdict == "APPROVE"
