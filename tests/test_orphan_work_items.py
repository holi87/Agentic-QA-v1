"""Ghost task rows whose spec file is missing on disk — list flags them,
prune removes them and cascades artifacts (issue #49)."""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from agentic_os.storage.db import connect
from agentic_os.work_items import (
    annotate_spec_status,
    create_work_item_from_payload,
    list_work_items,
    prune_orphan_work_items,
)
from tests.test_dashboard_task_ui import _DEFAULT_CONFIG, _free_port, _wait


def _runtime(tmp_path: Path, *, enable_write: bool = False) -> RuntimePaths:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_DEFAULT_CONFIG.format(write=str(enable_write).lower()).lstrip(), encoding="utf-8")
    conn = init_db(paths.db)
    Orchestrator(conn, paths, EventLog(conn, paths)).seed_phases()
    conn.close()
    return paths


def _seed_two_items(paths: RuntimePaths) -> tuple[str, str]:
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        kept = create_work_item_from_payload(
            conn, paths, events, {"title": "kept on disk"}, default_sut_root=".",
        )["work_item"]["id"]
        orphan = create_work_item_from_payload(
            conn, paths, events, {"title": "ghost row"}, default_sut_root=".",
        )["work_item"]["id"]
        return kept, orphan
    finally:
        conn.close()


def test_annotate_spec_status_flags_missing_files(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    kept_id, orphan_id = _seed_two_items(paths)
    # Remove the spec file out of band — simulates the bug operator hit.
    (paths.task_specs_dir / f"{orphan_id}.md").unlink()
    conn = connect(paths.db)
    try:
        items = annotate_spec_status(paths, list_work_items(conn))
    finally:
        conn.close()
    by_id = {item["id"]: item for item in items}
    assert by_id[kept_id]["spec_missing"] is False
    assert by_id[orphan_id]["spec_missing"] is True


def test_prune_orphan_work_items_drops_rows_and_cascades_artifacts(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    kept_id, orphan_id = _seed_two_items(paths)
    (paths.task_specs_dir / f"{orphan_id}.md").unlink()
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        pruned = prune_orphan_work_items(conn, paths, events)
        assert [p["id"] for p in pruned] == [orphan_id]
        remaining = {row["id"] for row in list_work_items(conn)}
        assert remaining == {kept_id}
        # Artifacts cascaded.
        rows = conn.execute(
            "SELECT COUNT(*) FROM work_item_artifacts WHERE work_item_id=?;",
            (orphan_id,),
        ).fetchone()
        assert rows[0] == 0
    finally:
        conn.close()


def test_prune_orphan_work_items_refuses_to_drop_rows_with_specs(tmp_path: Path) -> None:
    """ids= filter must still verify the spec is missing — never drop a row
    whose file is still on disk."""
    paths = _runtime(tmp_path)
    kept_id, _ = _seed_two_items(paths)
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        pruned = prune_orphan_work_items(conn, paths, events, ids=[kept_id])
        assert pruned == []
        assert {row["id"] for row in list_work_items(conn)} >= {kept_id}
    finally:
        conn.close()


def test_api_tasks_reports_orphans_and_prune_endpoint_removes_them(tmp_path: Path) -> None:
    paths = _runtime(tmp_path, enable_write=True)
    kept_id, orphan_id = _seed_two_items(paths)
    (paths.task_specs_dir / f"{orphan_id}.md").unlink()
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()
        with urllib.request.urlopen(base + "/api/tasks", timeout=4) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["orphans"] == 1
        by_id = {t["id"]: t for t in payload["tasks"]}
        assert by_id[orphan_id]["spec_missing"] is True
        assert by_id[kept_id]["spec_missing"] is False

        req = urllib.request.Request(
            base + "/api/tasks/prune",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            prune_payload = json.loads(resp.read().decode("utf-8"))
        assert prune_payload["count"] == 1
        assert prune_payload["pruned"][0]["id"] == orphan_id

        with urllib.request.urlopen(base + "/api/tasks", timeout=4) as resp:
            after = json.loads(resp.read().decode("utf-8"))
        assert after["orphans"] == 0
        assert [t["id"] for t in after["tasks"]] == [kept_id]
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_prune_endpoint_blocked_when_writes_disabled(tmp_path: Path) -> None:
    paths = _runtime(tmp_path, enable_write=False)
    _seed_two_items(paths)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()
        req = urllib.request.Request(
            base + "/api/tasks/prune",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=4)
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            assert exc.code == 403
            assert body["error"] == "dashboard_write_disabled"
        else:
            raise AssertionError("prune should have been forbidden")
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)
