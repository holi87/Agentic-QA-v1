"""Issue #273 — cross-run learnings store (record, decay, read helpers, CLI)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_os import learnings
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


def _conn(tmp_path: Path):
    paths = RuntimePaths(
        repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os"
    )
    paths.ensure()
    return init_db(paths.db), paths


def test_migration_creates_learnings_table(tmp_path):
    conn, _ = _conn(tmp_path)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='learnings';"
    ).fetchone()
    assert row is not None
    # UNIQUE index present so re-observe can upsert.
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='learnings_kind_subject_idx';"
    ).fetchone()
    assert idx is not None


def test_migration_v12_to_v13(tmp_path):
    """An existing v12 DB gains the learnings table via the incremental path."""
    from agentic_os.storage import db as db_mod

    db_path = tmp_path / "legacy.db"
    conn = db_mod.connect(db_path)
    conn.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);"
        # A real v12 DB already has work_items (m2) and autonomy_sessions (m9);
        # the v14 projects migration ALTERs both, so the fixture must carry them.
        "CREATE TABLE work_items (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL);"
        "CREATE TABLE autonomy_sessions (id TEXT PRIMARY KEY, started_at TEXT NOT NULL);"
        "INSERT INTO schema_migrations(version, name, applied_at) "
        "VALUES (12, 'pre', '2026-01-01T00:00:00Z');"
    )
    assert db_mod.current_version(conn) == 12
    db_mod.migrate(conn)
    assert db_mod.current_version(conn) == db_mod.SCHEMA_VERSION
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
    }
    assert "learnings" in names
    conn.close()


def test_record_and_get(tmp_path):
    conn, _ = _conn(tmp_path)
    learnings.record_learning(
        conn, kind="flaky", subject="feat.feature::login", payload={"n": 1}, actor="triager"
    )
    rows = learnings.list_learnings(conn)
    assert len(rows) == 1
    got = learnings.get_learning(conn, rows[0]["id"])
    assert got["kind"] == "flaky"
    assert got["subject"] == "feat.feature::login"
    assert got["payload"] == {"n": 1}
    assert got["weight"] == 1.0
    assert got["actor"] == "triager"


def test_reobserve_resets_recency_not_count(tmp_path):
    """Synthetic flaky observed 3x: one row, weight stays 1.0, observed_at moves."""
    conn, _ = _conn(tmp_path)
    subject = "feat.feature::checkout"
    stamps = [
        "2026-05-01T10:00:00.000Z",
        "2026-05-10T10:00:00.000Z",
        "2026-05-20T10:00:00.000Z",
    ]
    for i, stamp in enumerate(stamps, start=1):
        learnings.record_learning(
            conn, kind="flaky", subject=subject, payload={"seen": i},
            actor="triager", observed_at=stamp,
        )
    rows = learnings.list_learnings(conn, kind="flaky")
    assert len(rows) == 1  # one row, not three
    assert rows[0]["weight"] == 1.0
    assert rows[0]["observed_at"] == stamps[-1]  # latest observation wins
    assert rows[0]["payload"] == {"seen": 3}


def test_unknown_kind_rejected(tmp_path):
    conn, _ = _conn(tmp_path)
    with pytest.raises(ValueError):
        learnings.record_learning(
            conn, kind="triage_pattern", subject="x", payload={}, actor="triager"
        )


def test_decay_recomputes_and_prunes(tmp_path):
    conn, _ = _conn(tmp_path)
    now = datetime(2026, 5, 27, tzinfo=timezone.utc)
    fresh = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    learnings.record_learning(
        conn, kind="flaky", subject="a::1", payload={}, actor="t", observed_at=fresh
    )
    learnings.record_learning(
        conn, kind="flaky", subject="b::2", payload={}, actor="t", observed_at=old
    )
    res = learnings.decay_learnings(conn, now=now)
    # 60 days at tau=14 → exp(-60/14) ≈ 0.014 < 0.05 → pruned.
    assert res["pruned"] == 1
    assert res["recomputed"] == 1
    remaining = learnings.list_learnings(conn, kind="flaky")
    assert [r["subject"] for r in remaining] == ["a::1"]
    assert remaining[0]["weight"] == pytest.approx(1.0, abs=1e-3)


def test_forget_reverts_to_default(tmp_path):
    conn, _ = _conn(tmp_path)
    learnings.record_learning(
        conn, kind="flaky", subject="feat::scn", payload={}, actor="t"
    )
    assert learnings.flaky_subjects(conn) == ["feat::scn"]
    lid = learnings.list_learnings(conn)[0]["id"]
    assert learnings.forget_learning(conn, lid) is True
    assert learnings.flaky_subjects(conn) == []  # consult reverts to default
    assert learnings.forget_learning(conn, lid) is False  # idempotent


def test_flaky_subjects_filters_by_weight(tmp_path):
    conn, _ = _conn(tmp_path)
    now = datetime(2026, 5, 27, tzinfo=timezone.utc)
    old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    learnings.record_learning(
        conn, kind="flaky", subject="fresh::1", payload={}, actor="t"
    )
    learnings.record_learning(
        conn, kind="flaky", subject="stale::2", payload={}, actor="t", observed_at=old
    )
    learnings.decay_learnings(conn, now=now)
    assert learnings.flaky_subjects(conn) == ["fresh::1"]


def test_provider_quality_scores(tmp_path):
    conn, _ = _conn(tmp_path)
    learnings.record_learning(
        conn, kind="provider_quality", subject="reviewer::claude",
        payload={"ok": 12, "total": 12}, actor="orchestrator",
    )
    learnings.record_learning(
        conn, kind="provider_quality", subject="reviewer::codex",
        payload={"ok": 3, "total": 9}, actor="orchestrator",
    )
    scores = learnings.provider_quality_scores(conn, role="reviewer")
    assert set(scores) == {"claude", "codex"}
    assert scores["claude"] == 1.0


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "sut:\n  root: .\nmodels: {}\ndashboard:\n  enable_write_endpoints: false\n",
        encoding="utf-8",
    )
    return repo


def test_cli_list_show_forget_decay(cli_repo: Path, capsys) -> None:
    import json as _json

    from agentic_os.cli import cmd_learnings, open_runtime

    # Seed one learning through the storage layer the orchestrator uses.
    conn, _paths, _events, _orch = open_runtime(cli_repo)
    learnings.record_learning(
        conn, kind="flaky", subject="feat::scn", payload={"n": 1}, actor="triager"
    )
    conn.close()

    rc = cmd_learnings(cli_repo, ["list", "--kind", "flaky"], json_output=True)
    out = capsys.readouterr().out
    assert rc == 0
    rows = _json.loads(out)["learnings"]
    assert len(rows) == 1
    lid = rows[0]["id"]

    rc = cmd_learnings(cli_repo, ["show", str(lid)], json_output=True)
    assert rc == 0
    capsys.readouterr()

    rc = cmd_learnings(cli_repo, ["decay"], json_output=True)
    assert rc == 0
    capsys.readouterr()

    rc = cmd_learnings(cli_repo, ["forget", str(lid)], json_output=True)
    assert rc == 0
    capsys.readouterr()

    rc = cmd_learnings(cli_repo, ["list"], json_output=True)
    out = capsys.readouterr().out
    assert _json.loads(out)["learnings"] == []


# ---------------------------------------------------------------------------
# Read-site acceptance: planner quarantines flaky scenarios; forget reverts.
# ---------------------------------------------------------------------------


def _planner_runtime(tmp_path: Path):
    from agentic_os.events import EventLog
    from agentic_os.orchestrator import Orchestrator

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    orch = Orchestrator(conn, paths, EventLog(conn, paths))
    orch.seed_phases()
    return conn, paths, EventLog(conn, paths)


def _seed_planned_work_item(conn, paths, events) -> str:
    from agentic_os.work_items import create_work_item_from_payload

    detail = create_work_item_from_payload(
        conn,
        paths,
        events,
        {
            "title": "Flaky-aware plan",
            "spec_path": "specs/x.md",
            "priority": "P1",
            "sut_root": ".",
            "scenarios": ["s"],
        },
        default_sut_root=".",
    )
    wid = str(detail["work_item"]["id"])
    analysis_dir = paths.runtime_root / "analysis" / wid
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "requirements.md").write_text("# requirements\n", encoding="utf-8")
    (analysis_dir / "candidate-tests.md").write_text("# candidates\n- C1\n", encoding="utf-8")
    (analysis_dir / "candidate-tests.json").write_text(
        '{"items": [{"candidate_id": "C1", "title": "t", "test_type": "api"}]}',
        encoding="utf-8",
    )
    (analysis_dir / "sut-map.json").write_text('{"openapi_inventory": []}', encoding="utf-8")
    return wid


def test_planner_quarantines_flaky_then_forget_reverts(tmp_path):
    from agentic_os.test_planning import plan_work_item

    conn, paths, events = _planner_runtime(tmp_path)
    wid = _seed_planned_work_item(conn, paths, events)

    # No learnings yet → nothing quarantined.
    result = plan_work_item(conn, paths, events, work_item_id=wid)
    assert result["plan_summary"]["quarantine"] == []

    # Observe a flaky scenario; next plan run surfaces it as quarantined and
    # emits the learning.consulted audit event.
    learnings.record_learning(
        conn, kind="flaky", subject="x.feature::flaky-scn", payload={}, actor="triager"
    )
    result = plan_work_item(conn, paths, events, work_item_id=wid)
    assert result["plan_summary"]["quarantine"] == ["x.feature::flaky-scn"]
    consulted = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='learning.consulted' AND actor='planner';"
    ).fetchone()[0]
    assert consulted >= 1

    # Operator forget → decision reverts to default (nothing quarantined).
    lid = learnings.list_learnings(conn, kind="flaky")[0]["id"]
    learnings.forget_learning(conn, lid)
    result = plan_work_item(conn, paths, events, work_item_id=wid)
    assert result["plan_summary"]["quarantine"] == []


# ---------------------------------------------------------------------------
# Dashboard surface (live HTTP).
# ---------------------------------------------------------------------------


@pytest.fixture
def live_learnings(tmp_path):
    import threading

    from agentic_os.server import make_server
    from agentic_os.storage.db import transaction

    from test_dashboard_server import _free_port, _runtime  # type: ignore[import-not-found]

    paths = _runtime(tmp_path, enable_write=True)
    conn = init_db(paths.db)
    with transaction(conn):
        conn.execute(
            "INSERT INTO learnings(kind, subject, payload, observed_at, weight, actor) "
            "VALUES ('flaky', 'feat::scn', '{}', '2026-05-20T10:00:00.000Z', 0.8, 'triager');"
        )
    lid = conn.execute("SELECT id FROM learnings;").fetchone()[0]
    conn.close()
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"host": "127.0.0.1", "port": port, "lid": lid}
    finally:
        srv.shutdown()
        srv.server_close()


def _get_json(host, port, path):
    import urllib.request

    with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5) as resp:
        import json as _json

        return _json.loads(resp.read().decode("utf-8"))


def test_learnings_page_served(live_learnings):
    import urllib.request

    url = f"http://{live_learnings['host']}:{live_learnings['port']}/learnings"
    with urllib.request.urlopen(url, timeout=5) as resp:
        html = resp.read().decode("utf-8")
    assert 'id="learnings-table"' in html


def test_learnings_api_list_and_detail(live_learnings):
    host, port = live_learnings["host"], live_learnings["port"]
    payload = _get_json(host, port, "/api/learnings")
    assert payload["count"] == 1
    assert payload["learnings"][0]["subject"] == "feat::scn"
    detail = _get_json(host, port, f"/api/learnings/{live_learnings['lid']}")
    assert detail["learning"]["kind"] == "flaky"


def test_learnings_api_forget(live_learnings):
    import urllib.request

    host, port = live_learnings["host"], live_learnings["port"]
    lid = live_learnings["lid"]
    req = urllib.request.Request(
        f"http://{host}:{port}/api/learnings/{lid}/forget", method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
    payload = _get_json(host, port, "/api/learnings")
    assert payload["count"] == 0
