"""Orchestration page and paginated step-history endpoint behavior."""
from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from pathlib import Path

import pytest

from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "scripts" / "agentic-os" / "templates"
STATIC_DIR = TEMPLATES_DIR / "static"


def _runtime(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    return conn, paths, events, orch


def _seed_steps(events: EventLog, n: int) -> None:
    for i in range(n):
        sid = events.start_step(
            kind="planner",
            phase="analyze",
            actor="planner-autopilot",
            role="planner",
            provider="claude",
            skill="analyze-task",
            work_item_id=f"WI-{i}",
        )
        events.end_step(
            sid,
            outcome="ok",
            kind="planner",
            phase="analyze",
            actor="planner-autopilot",
            role="planner",
            provider="claude",
            work_item_id=f"WI-{i}",
        )


def _get_json(host: str, port: int, path: str) -> dict:
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.fixture
def live(tmp_path):
    conn, paths, events, _orch = _runtime(tmp_path)
    _seed_steps(events, 5)
    httpd = make_server(paths, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        yield {"host": host, "port": port, "conn": conn, "events": events}
    finally:
        httpd.shutdown()
        httpd.server_close()
        conn.close()


def test_events_history_filters_step_kind(live) -> None:
    payload = _get_json(live["host"], live["port"], "/api/events/history?kind=step.*&limit=50")
    assert "events" in payload
    assert payload["count"] >= 10  # 5 steps × (start + end)
    assert all(e["kind"].startswith("step.") for e in payload["events"])


def test_events_history_before_paginates(live) -> None:
    first = _get_json(live["host"], live["port"], "/api/events/history?kind=step.*&limit=4")
    assert first["count"] == 4
    oldest = min(e["id"] for e in first["events"])
    page2 = _get_json(
        live["host"], live["port"], f"/api/events/history?kind=step.*&limit=4&before={oldest}"
    )
    # All page2 ids strictly less than the oldest from page1.
    assert all(e["id"] < oldest for e in page2["events"])


def test_orchestration_page_served(live) -> None:
    url = f"http://{live['host']}:{live['port']}/orchestration"
    with urllib.request.urlopen(url, timeout=5) as resp:
        html = resp.read().decode("utf-8")
    assert "phase-machine" in html
    assert "/static/orchestration.js" in html
    assert 'id="step-timeline"' in html


def test_orchestration_js_has_no_innerhtml() -> None:
    body = (STATIC_DIR / "orchestration.js").read_text(encoding="utf-8")
    assert "innerHTML" not in body, "use safe DOM methods, not innerHTML"


def test_orchestration_template_lists_all_phases() -> None:
    body = (STATIC_DIR / "orchestration.js").read_text(encoding="utf-8")
    for phase in ("analyze", "design", "implement", "review", "triage", "generate", "gate", "run", "report"):
        assert f"'{phase}'" in body
