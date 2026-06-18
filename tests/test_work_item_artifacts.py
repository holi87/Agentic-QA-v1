from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentic_os.events import EventLog
from agentic_os.errors import UsageError
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.work_items import (
    create_work_item_from_file,
    create_work_item_from_payload,
    get_work_item_detail,
    list_work_items,
    register_work_item_artifact,
)


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    Orchestrator(conn, paths, EventLog(conn, paths)).seed_phases()
    return conn, paths, EventLog(conn, paths)


def test_create_work_item_from_file_copies_spec_and_registers_artifact(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    spec = paths.repo_root / "examples" / "tasks" / "order-negative.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(
        "# Order negative validation\n\nPriority: P1\nSUT root: .\n\n## Expected behavior\nReject invalid order data.\n",
        encoding="utf-8",
    )
    try:
        detail = create_work_item_from_file(
            conn,
            paths,
            events,
            Path("examples/tasks/order-negative.md"),
            default_sut_root=".",
        )
        item = detail["work_item"]
        assert item["id"].startswith("TASK-")
        assert item["status"] == "queued"
        assert item["priority"] == "P1"
        assert item["spec_path"].startswith(".agentic-os/task-specs/")
        copied = paths.repo_root / item["spec_path"]
        assert copied.exists()
        assert "Order negative validation" in copied.read_text(encoding="utf-8")
        assert detail["artifacts"][0]["kind"] == "spec"
        assert detail["artifacts"][0]["path"] == item["spec_path"]
    finally:
        conn.close()


def test_work_item_rejects_path_traversal(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    try:
        with pytest.raises(UsageError, match="escapes repo root"):
            create_work_item_from_file(conn, paths, events, Path("../outside.md"))
    finally:
        conn.close()


def test_payload_create_and_artifact_registry_validate_paths(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        detail = create_work_item_from_payload(
            conn,
            paths,
            events,
            {
                "title": "Checkout API coverage",
                "priority": "P2",
                "business_goal": "Cover checkout failures.",
                "expected_behavior": "Invalid payment data is rejected.",
            },
            default_sut_root=".",
        )
        item = detail["work_item"]
        assert list_work_items(conn)[0]["id"] == item["id"]
        artifact = register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=item["id"],
            kind="analysis",
            path=".agentic-os/analysis/" + item["id"] + "/requirements.md",
        )
        assert artifact["kind"] == "analysis"
        refreshed = get_work_item_detail(conn, item["id"])
        assert refreshed is not None
        assert [a["kind"] for a in refreshed["artifacts"]] == ["spec", "analysis"]
    finally:
        conn.close()


def test_payload_create_rejects_sut_root_escape(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        with pytest.raises(UsageError, match="escapes repo root"):
            create_work_item_from_payload(
                conn,
                paths,
                events,
                {"title": "Bad root", "sut_root": "../sut"},
            )
    finally:
        conn.close()
