"""Session history migrations, indexing, replay windows, comparisons, bookmarks, and retention."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from agentic_os import sessions as sessions_mod
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from agentic_os.storage.db import transaction

from test_dashboard_server import _runtime, _free_port  # type: ignore[import-not-found]


# ---- migration ----


def test_migration_creates_session_tables(tmp_path: Path) -> None:
    paths = RuntimePaths(repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    try:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        ).fetchall()}
        assert "autonomy_sessions" in names
        assert "session_bookmarks" in names
    finally:
        conn.close()


def test_migration_v8_to_v9(tmp_path: Path) -> None:
    from agentic_os.storage import db as db_mod

    db_path = tmp_path / "legacy.db"
    conn = db_mod.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
        -- A real v8 DB already has work_items (migration 2); the v14 projects
        -- migration ALTERs it, so the synthetic fixture must carry it too.
        CREATE TABLE work_items (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL);
        INSERT INTO schema_migrations(version, name, applied_at) VALUES (8, 'pre', '2026-01-01T00:00:00Z');
        """
    )
    assert db_mod.current_version(conn) == 8
    db_mod.migrate(conn)
    # Migrating from v8 applies the v9 (session) migration en route to HEAD.
    assert db_mod.current_version(conn) == db_mod.SCHEMA_VERSION
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table';"
    ).fetchall()}
    assert "autonomy_sessions" in names
    assert "session_bookmarks" in names
    conn.close()


# ---- module helpers ----


def _seed(conn, sid, started, finished, status="finished", mode="loop"):
    sessions_mod.record_session_start(
        conn, session_id=sid, started_at=started, mode=mode, max_minutes=60
    )
    if finished:
        sessions_mod.finalize_session(
            conn, session_id=sid, status=status, finished_at=finished,
            work_items_processed=3, blocks=1, failures=2,
        )


def test_record_list_get(tmp_path: Path) -> None:
    paths = RuntimePaths(repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    try:
        _seed(conn, "S1", "2026-05-26T10:00:00Z", "2026-05-26T11:00:00Z")
        _seed(conn, "S2", "2026-05-26T12:00:00Z", None, status="running")
        rows = sessions_mod.list_sessions(conn)
        assert [r["id"] for r in rows] == ["S2", "S1"]  # newest first
        got = sessions_mod.get_session(conn, "S1")
        assert got["work_items_processed"] == 3
        assert got["blocks"] == 1
        assert got["failures"] == 2
    finally:
        conn.close()


def test_list_filters(tmp_path: Path) -> None:
    paths = RuntimePaths(repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    try:
        _seed(conn, "S1", "2026-05-26T10:00:00Z", "2026-05-26T11:00:00Z", status="finished")
        _seed(conn, "S2", "2026-05-26T12:00:00Z", None, status="running")
        assert [r["id"] for r in sessions_mod.list_sessions(conn, status="running")] == ["S2"]
        assert [r["id"] for r in sessions_mod.list_sessions(conn, status="finished")] == ["S1"]
    finally:
        conn.close()


def test_bookmark_set_and_clear(tmp_path: Path) -> None:
    paths = RuntimePaths(repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    try:
        _seed(conn, "S1", "2026-05-26T10:00:00Z", "2026-05-26T11:00:00Z")
        assert sessions_mod.set_bookmark(conn, "S1", "regression") is True
        assert sessions_mod.get_session(conn, "S1")["bookmark"] == "regression"
        # clearing with empty label drops the row
        sessions_mod.set_bookmark(conn, "S1", "")
        assert sessions_mod.get_session(conn, "S1")["bookmark"] is None
        # unknown session
        assert sessions_mod.set_bookmark(conn, "NOPE", "x") is False
    finally:
        conn.close()


def test_compare(tmp_path: Path) -> None:
    paths = RuntimePaths(repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    try:
        _seed(conn, "S1", "2026-05-26T10:00:00Z", "2026-05-26T11:00:00Z")
        sessions_mod.finalize_session(conn, session_id="S1", status="finished",
                                      work_items_processed=2, blocks=0, failures=1)
        _seed(conn, "S2", "2026-05-26T12:00:00Z", "2026-05-26T13:00:00Z")
        sessions_mod.finalize_session(conn, session_id="S2", status="finished",
                                      work_items_processed=5, blocks=2, failures=0)
        cmp = sessions_mod.compare_sessions(conn, "S1", "S2")
        assert cmp["fields"]["work_items_processed"]["delta"] == 3
        assert cmp["fields"]["failures"]["delta"] == -1
    finally:
        conn.close()


def test_counts_from_events_log() -> None:
    log = [
        {"step": "analyze:WI-1", "ok": True},
        {"step": "plan:WI-1", "ok": True},
        {"step": "implement:WI-2", "ok": False},
        {"step": "WI-3:awaiting_operator_decision", "ok": False},
    ]
    counts = sessions_mod.counts_from_events_log(log)
    assert counts["work_items_processed"] == 2  # WI-1, WI-2 (WI-3 form differs)
    assert counts["blocks"] == 1
    assert counts["failures"] == 1


def test_retention_sweep_archives_old_ndjson(tmp_path: Path) -> None:
    paths = RuntimePaths(repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os")
    paths.ensure()
    old = paths.events_dir / "2020-01-01.ndjson"
    old.write_text('{"kind":"x"}\n', encoding="utf-8")
    import os
    old_ts = time.time() - 200 * 86400
    os.utime(old, (old_ts, old_ts))
    fresh = paths.events_dir / "9999-01-01.ndjson"
    fresh.write_text('{"kind":"y"}\n', encoding="utf-8")

    result = sessions_mod.sweep_retention(paths, retention_days=30)
    assert any("2020-01-01.ndjson" in m for m in result["moved"])
    assert not old.exists()
    assert fresh.exists()
    # Archived under <runtime>/archive/<mtime-month>/ (bucket follows mtime).
    archived = list((paths.runtime_root / "archive").rglob("2020-01-01.ndjson"))
    assert len(archived) == 1


# ---- endpoints ----


def _get_json(host, port, path):
    with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(host, port, path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture
def live(tmp_path):
    paths = _runtime(tmp_path, enable_write=True)
    conn = init_db(paths.db)
    with transaction(conn):
        _seed(conn, "S1", "2026-05-26T10:00:00Z", "2026-05-26T11:00:00Z")
    conn.close()
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"host": "127.0.0.1", "port": port, "paths": paths}
    finally:
        srv.shutdown()
        srv.server_close()


def test_sessions_list_endpoint(live) -> None:
    payload = _get_json(live["host"], live["port"], "/api/sessions")
    assert payload["count"] >= 1
    assert payload["sessions"][0]["id"] == "S1"


def test_session_detail_endpoint(live) -> None:
    payload = _get_json(live["host"], live["port"], "/api/sessions/S1")
    assert payload["session"]["id"] == "S1"


def test_session_bookmark_endpoint(live) -> None:
    import urllib.error
    status, body = _post_json(live["host"], live["port"], "/api/sessions/S1/bookmark", {"label": "tag-x"})
    assert status == 200
    assert body["ok"] is True
    payload = _get_json(live["host"], live["port"], "/api/sessions/S1")
    assert payload["session"]["bookmark"] == "tag-x"


def test_sessions_page_served(live) -> None:
    with urllib.request.urlopen(f"http://{live['host']}:{live['port']}/sessions", timeout=5) as resp:
        html = resp.read().decode("utf-8")
    assert 'id="sessions-table"' in html
    assert "/static/sessions.js" in html


# ---- issue #265: session counters survive ring-buffer truncation ----


def test_session_counters_survive_ring_truncation() -> None:
    """Long sessions must finalize accurate counts even after the bounded
    events_log drops old entries — a re-scan would undercount."""
    from agentic_os import autonomy
    from agentic_os.runtime.tuning import EVENTS_LOG_RING_SIZE

    session = autonomy._SessionState(
        session_id="autonomy-test",
        started_at="2026-05-27T00:00:00Z",
        expected_finish_at="2026-05-27T01:00:00Z",
        max_minutes=60,
    )
    distinct = EVENTS_LOG_RING_SIZE + 50
    for i in range(distinct):
        autonomy._record(session, f"run:WI-{i:05d}", True)
    autonomy._record(session, "blocked:WI-block", True)
    autonomy._record(session, "run:WI-fail", False)

    # The ring buffer truncated, so a re-scan undercounts.
    assert len(session.events_log) == EVENTS_LOG_RING_SIZE
    rescan = sessions_mod.counts_from_events_log(list(session.events_log))
    assert rescan["work_items_processed"] < distinct

    # The durable counters retain the full totals.
    assert len(session.processed_work_items) == distinct + 2
    assert session.block_count == 1
    assert session.failure_count == 1
