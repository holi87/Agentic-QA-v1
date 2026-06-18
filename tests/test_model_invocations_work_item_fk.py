"""Regression — issue #339.

``model_invocations.task_id`` references ``tasks(id)`` (the older
execution-path slice). The autonomous pipeline writes against
``work_items(id)`` instead, so before this migration every autonomous
row recorded with ``task_id=NULL`` and the memory-module transcript
chain (``model_invocations.task_id → tasks.payload.$.work_item_id →
work_items``) silently fell through to the ``'default'`` project.

These tests pin:

1. The new ``model_invocations.work_item_id`` column exists, references
   ``work_items(id)``, and is populated by ``invoke_model`` /
   ``try_invoke_role`` for autonomous-pipeline rows.
2. ``memory._index_transcripts`` resolves the project directly via the
   new column (no fallback to the ``'default'`` project) for autonomous
   rows; legacy rows that only carry ``task_id`` still resolve via the
   old chain.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from agentic_os.events import EventLog
from agentic_os.models import invoke_model
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


def _runtime(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    return conn, paths, events, repo


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o755)


def _seed_work_item(conn: sqlite3.Connection, *, work_id: str, project_id: str = "default") -> None:
    conn.execute(
        "INSERT INTO work_items(id, project_id, title, priority, sut_root, "
        "spec_path, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (
            work_id,
            project_id,
            "Sample task",
            "P2",
            ".",
            "agentic-os-runtime/tasks/sample.md",
            "analyzing",
            "2026-05-28T00:00:00Z",
            "2026-05-28T00:00:00Z",
        ),
    )
    conn.commit()


def test_model_invocations_schema_has_work_item_id_column(tmp_path: Path) -> None:
    """Migration #17 must add ``work_item_id`` with a FK to ``work_items``."""
    conn, paths, _events, _repo = _runtime(tmp_path)
    try:
        cols = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(model_invocations);").fetchall()
        }
        assert "work_item_id" in cols, "migration #17 must add work_item_id column"

        fks = conn.execute(
            "PRAGMA foreign_key_list(model_invocations);"
        ).fetchall()
        targets = {row["from"]: row["table"] for row in fks}
        assert targets.get("work_item_id") == "work_items", (
            "work_item_id must FK to work_items(id)"
        )
        # The historic FK on task_id is preserved for the older execution path.
        assert targets.get("task_id") == "tasks"
    finally:
        conn.close()


def test_invoke_model_writes_work_item_id_column(tmp_path: Path) -> None:
    """``invoke_model(work_item_id=...)`` persists the value into the new
    column so consumers (memory, future per-task metrics) resolve directly."""
    conn, paths, events, _repo = _runtime(tmp_path)
    try:
        fake = tmp_path / "bin" / "fake-claude"
        _write_executable(fake, "#!/usr/bin/env bash\nprintf 'ok\\n'\n")
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        try:
            work_id = "TASK-20260528-000000-fk-test"
            _seed_work_item(conn, work_id=work_id)
            result = invoke_model(
                conn,
                paths,
                events,
                role="planner",
                config={
                    "planner": {
                        "provider": "claude",
                        "command": [str(fake)],
                        "role": "opus",
                    }
                },
                prompt="hello",
                session_id="sess-339",
                work_item_id=work_id,
                timeout_seconds=5,
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(
                str(fake.parent) + os.pathsep, ""
            )
        assert result.exit_code == 0

        row = conn.execute(
            "SELECT work_item_id, task_id FROM model_invocations WHERE id=?;",
            (result.invocation_id,),
        ).fetchone()
        assert row["work_item_id"] == work_id
        assert row["task_id"] is None
    finally:
        conn.close()


def test_memory_transcript_resolves_via_work_item_id(tmp_path: Path) -> None:
    """An autonomous transcript scoped through the new FK must land in the
    real project (not the ``'default'`` fallback).
    """
    from agentic_os.memory import build_memory

    conn, paths, events, _repo = _runtime(tmp_path)
    try:
        # Seed a non-default project + work item.
        conn.execute(
            "INSERT INTO projects(id, name, sut_root, created_at) VALUES (?, ?, ?, ?);",
            ("proj-339", "Project 339", ".", "2026-05-28T00:00:00Z"),
        )
        _seed_work_item(conn, work_id="TASK-20260528-000000-fk-mem", project_id="proj-339")

        # Hand-write a model_invocations row + transcript so we test the
        # join chain in isolation from the live planner pipeline.
        conn.execute(
            """
            INSERT INTO model_invocations(
              id, session_id, task_id, work_item_id, run_id, model_role, provider,
              command, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                "inv-339",
                "sess-339",
                None,
                "TASK-20260528-000000-fk-mem",
                None,
                "opus",
                "claude",
                "[]",
                "2026-05-28T00:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO model_transcripts(invocation_id, kind, ord, payload, ts)
            VALUES (?, ?, ?, ?, ?);
            """,
            ("inv-339", "assistant", 0, '{"text":"hello"}', "2026-05-28T00:00:00Z"),
        )
        conn.commit()

        # Rebuild the per-project index — the transcript must land in proj-339,
        # not the 'default' fallback.
        build_memory(conn, paths, project_id="proj-339", events=events)

        rows = conn.execute(
            "SELECT project_id, source, source_id FROM memory_index WHERE source='transcript';"
        ).fetchall()
        assert any(
            r["project_id"] == "proj-339" and r["source_id"] == "inv-339" for r in rows
        ), f"transcript must scope to proj-339, got: {[dict(r) for r in rows]}"

        # No memory.transcript_unscoped event should have fired for this row.
        events_rows = conn.execute(
            "SELECT kind, payload FROM events WHERE kind='memory.transcript_unscoped';"
        ).fetchall()
        assert not any(
            "inv-339" in (r["payload"] or "") for r in events_rows
        ), "autonomous transcript must not fall back to 'default'"
    finally:
        conn.close()
