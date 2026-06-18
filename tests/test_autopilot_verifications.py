"""Verification decisions, overrides, endpoints, migrations, and dashboard page behavior."""
from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentic_os import decisions as decisions_mod
from agentic_os.events import EventLog
from agentic_os.orchestrator import CURRENT_PHASE_ID, Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "scripts" / "agentic-os" / "templates" / "static"


def _runtime(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    return conn, paths, events, orch


def test_migration_adds_actor_column(tmp_path: Path) -> None:
    conn, _paths, _events, _orch = _runtime(tmp_path)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(decisions);").fetchall()]
    assert "actor" in cols
    conn.close()


def test_record_and_fetch_autopilot_decision(tmp_path: Path) -> None:
    conn, _paths, _events, _orch = _runtime(tmp_path)
    decisions_mod.record_decision(
        conn,
        phase_id=CURRENT_PHASE_ID,
        topic="candidate C1: generate_now",
        actor="planner-autopilot",
        rationale="coverage architect rule: read_only_api",
    )
    decisions_mod.record_decision(
        conn,
        phase_id=CURRENT_PHASE_ID,
        topic="operator override",
        actor="operator",
        rationale="manual",
    )
    auto = decisions_mod.fetch_decisions(conn, actor="*-autopilot")
    assert len(auto) == 1
    assert auto[0]["actor"] == "planner-autopilot"
    assert auto[0]["decided_by"] == "opus"  # constrained role mapping
    all_rows = decisions_mod.fetch_decisions(conn)
    assert len(all_rows) == 2
    conn.close()


def test_decided_by_check_constraint_still_holds(tmp_path: Path) -> None:
    conn, _paths, _events, _orch = _runtime(tmp_path)
    # Direct insert of an invalid decided_by must still fail.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decisions(id, phase_id, topic, decided_by, rationale, consequences, decided_at, actor) "
            "VALUES ('x', ?, 't', 'planner-autopilot', 'r', 'c', '2026-01-01T00:00:00Z', 'planner-autopilot');",
            (CURRENT_PHASE_ID,),
        )
    conn.close()


def _get_json(host, port, path):
    url = f"http://{host}:{port}{path}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(host, port, path, body):
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture
def live(tmp_path):
    conn, paths, events, _orch = _runtime(tmp_path)
    decisions_mod.record_decision(
        conn, phase_id=CURRENT_PHASE_ID, topic="auto", actor="triager-autopilot", rationale="r"
    )
    httpd = make_server(paths, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        yield {"host": host, "port": port, "conn": conn, "paths": paths}
    finally:
        httpd.shutdown()
        httpd.server_close()
        conn.close()


def test_decisions_endpoint_returns_rows(live) -> None:
    payload = _get_json(live["host"], live["port"], "/api/decisions?limit=10")
    assert payload["count"] >= 1
    assert payload["decisions"][0]["actor"] == "triager-autopilot"


def test_decisions_actor_filter(live) -> None:
    auto = _get_json(live["host"], live["port"], "/api/decisions?actor=*-autopilot")
    assert all("-autopilot" in d["actor"] for d in auto["decisions"])


def test_override_blocked_when_writes_disabled(live) -> None:
    rows = _get_json(live["host"], live["port"], "/api/decisions")["decisions"]
    did = rows[0]["id"]
    status, body = _post_json(live["host"], live["port"], f"/api/decisions/{did}/override", {"note": "x"})
    assert status == 403
    assert body["error"] == "dashboard_write_disabled"


def test_verifications_page_served(live) -> None:
    url = f"http://{live['host']}:{live['port']}/verifications"
    with urllib.request.urlopen(url, timeout=5) as resp:
        html = resp.read().decode("utf-8")
    assert 'id="verif-timeline"' in html
    assert "/static/verifications.js" in html


def test_verifications_js_no_innerhtml() -> None:
    body = (STATIC_DIR / "verifications.js").read_text(encoding="utf-8")
    assert "innerHTML" not in body


def test_parse_detail_covers_strict_reviewer_format() -> None:
    """The verifications.js parser must recognise the strict reviewer keys
    documented in config/prompts/reviewer.md (verdict / reason / findings)
    plus the triager fields (severity / priority / owasp / iso25010)."""
    body = (STATIC_DIR / "verifications.js").read_text(encoding="utf-8")
    # The parse regex is the contract; assert every strict key is in it.
    for key in ("verdict", "reason", "findings", "severity", "priority", "owasp", "iso25010"):
        assert key in body, f"parseDetail missing strict key: {key}"


def test_migration_v7_to_v8_adds_actor_and_preserves_rows(tmp_path: Path) -> None:
    """A DB stamped at version 7 (decisions without `actor`) must gain the
    column on upgrade without dropping existing decision rows."""
    from agentic_os.storage import db as db_mod

    db_path = tmp_path / "legacy.db"
    conn = db_mod.connect(db_path)
    # Minimal old-shape schema: phases + decisions WITHOUT actor + migrations.
    conn.executescript(
        """
        CREATE TABLE phases (id TEXT PRIMARY KEY);
        CREATE TABLE decisions (
          id TEXT PRIMARY KEY,
          phase_id TEXT NOT NULL REFERENCES phases(id),
          topic TEXT NOT NULL,
          decided_by TEXT NOT NULL CHECK (decided_by IN ('opus','sonnet','codex','operator','script')),
          rationale TEXT NOT NULL,
          consequences TEXT NOT NULL,
          reversed_by TEXT REFERENCES decisions(id),
          decided_at TEXT NOT NULL
        );
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
        -- A real v7 DB already has work_items (migration 2); the v14 projects
        -- migration ALTERs it, so the synthetic fixture must carry it too.
        CREATE TABLE work_items (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL);
        INSERT INTO schema_migrations(version, name, applied_at) VALUES (7, 'pre', '2026-01-01T00:00:00Z');
        INSERT INTO phases(id) VALUES ('P1');
        INSERT INTO decisions(id, phase_id, topic, decided_by, rationale, consequences, decided_at)
          VALUES ('d1', 'P1', 'legacy', 'operator', 'r', 'c', '2026-01-01T00:00:00Z');
        """
    )
    assert db_mod.current_version(conn) == 7
    db_mod.migrate(conn)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(decisions);").fetchall()]
    assert "actor" in cols
    row = conn.execute("SELECT id, actor FROM decisions WHERE id='d1';").fetchone()
    assert row["id"] == "d1"
    assert row["actor"] == "operator"  # backfilled from decided_by
    conn.close()
