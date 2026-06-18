"""Dashboard task UI pages, static assets, config endpoint, and task spec endpoints."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from agentic_os.storage.db import connect
from agentic_os.work_items import (
    create_work_item_from_payload,
    list_work_items,
)


_DEFAULT_CONFIG = """
runtime:
  root: .agentic-os
  timezone: Europe/Warsaw
  max_parallel_tasks: 4
  heartbeat_seconds: 20
  lease_ttl_seconds: 60
  stale_lease_seconds: 90
  shutdown_grace_seconds: 5
  timeouts:
    default_seconds: 1800
    docker_seconds: 240
    test_seconds: 3600
    model_seconds: 1800
    report_seconds: 300
sut:
  root: .
  compose_file: docker-compose.yml
  compose_project_name: agentic-os-sut
  autostart: true
  healthcheck:
    command: ["curl", "-fsS", "http://127.0.0.1:3000/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  install_shim_allowed: false
models:
  planner:
    provider: claude
    command: ["claude", "--model", "opus"]
    role: opus
  implementer:
    provider: claude
    command: ["claude", "--model", "sonnet"]
    role: sonnet
  reviewer:
    provider: codex
    command: ["codex"]
    role: codex
dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: {write}
paths:
  reports: reports
  bugs: bugs
  evidence: evidence
  prompts: .qualitycat/prompts
reports:
  copy_reports_script: scripts/copy-reports.sh
  extract_last_run_script: scripts/extract-last-run.sh
  build_summary_script: scripts/build-summary.sh
  require_reports_on_failure: true
gates:
  known_bugs_fail_exit: true
  assertion_changes_require_decision: true
  exact_spec_failure_opens_bug: true
  require_functional_area_tag: true
  require_lifecycle_tag: true
  infrastructure_exit_code: 2
"""


def _runtime(tmp_path: Path, *, enable_write: bool = False) -> tuple[RuntimePaths, sqlite3.Connection]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_DEFAULT_CONFIG.format(write=str(enable_write).lower()).lstrip(), encoding="utf-8")
    conn = init_db(paths.db)
    Orchestrator(conn, paths, EventLog(conn, paths)).seed_phases()
    return paths, conn


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def server(tmp_path: Path):
    paths, conn = _runtime(tmp_path)
    conn.close()
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{port}", "paths": paths}
    srv._shutdown_requested = True  # type: ignore[attr-defined]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _seed_work_item(paths: RuntimePaths, title: str = "Order negative validation") -> str:
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        detail = create_work_item_from_payload(
            conn,
            paths,
            events,
            {
                "title": title,
                "priority": "P1",
                "business_goal": "Cover ordering happy + invalid paths.",
                "expected_behavior": "Invalid order data is rejected (POST /orders).",
                "relevant_surfaces": "POST /orders, /checkout",
            },
            default_sut_root=".",
        )
        return detail["work_item"]["id"]
    finally:
        conn.close()


def _wait(url: str, timeout: float = 5.0) -> urllib.request.addinfourl:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return urllib.request.urlopen(url, timeout=1)
        except (urllib.error.URLError, ConnectionRefusedError) as exc:
            last = exc
            time.sleep(0.1)
    raise AssertionError(f"server unreachable: {url}: {last}")


def test_application_view_serves_index(server: dict) -> None:
    with _wait(server["url"] + "/") as resp:
        body = resp.read().decode("utf-8")
        ctype = resp.headers.get("Content-Type", "")
    assert "text/html" in ctype
    assert "<title>Agentic OS — dashboard</title>" in body
    assert "Agentic OS" in body
    assert 'href="/tasks"' in body
    assert 'href="/tasks/new"' in body


def test_tasks_list_view_serves_html(server: dict) -> None:
    with _wait(server["url"] + "/tasks") as resp:
        body = resp.read().decode("utf-8")
    assert "<title>Tasks - Agentic OS</title>" in body
    assert 'id="work-items"' in body


def test_new_task_view_serves_html(server: dict) -> None:
    with _wait(server["url"] + "/tasks/new") as resp:
        body = resp.read().decode("utf-8")
    assert "<title>New task - Agentic OS</title>" in body
    assert 'id="task-form"' in body


def test_task_detail_view_serves_html(server: dict) -> None:
    with _wait(server["url"] + "/tasks/anything") as resp:
        body = resp.read().decode("utf-8")
    assert "<title>Task detail - Agentic OS</title>" in body
    assert 'id="task-id"' in body
    assert 'id="task-candidates"' in body


def test_agents_view_serves_editable_surface(server: dict) -> None:
    with _wait(server["url"] + "/agents") as resp:
        body = resp.read().decode("utf-8")
    assert "<title>Agents - Agentic OS</title>" in body
    assert 'id="agents-reload"' in body
    assert 'id="agents-write-status"' in body


def test_static_css_and_js_are_served(server: dict) -> None:
    with _wait(server["url"] + "/static/dashboard.css") as resp:
        ctype = resp.headers.get("Content-Type", "")
        body = resp.read().decode("utf-8")
    assert ctype.startswith("text/css")
    assert ".topbar" in body
    with _wait(server["url"] + "/static/dashboard.js") as resp:
        ctype = resp.headers.get("Content-Type", "")
        body = resp.read().decode("utf-8")
    assert ctype.startswith("application/javascript")
    assert "AgenticOS" in body
    assert "renderTaskCandidates" in body


def test_config_endpoint_exposes_sut_and_gate(server: dict) -> None:
    with _wait(server["url"] + "/api/config") as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    assert payload["source"] == ".qualitycat/agentic-os.yml"
    assert payload["sut"]["root"] == "."
    assert payload["sut"]["compose_file"] == "docker-compose.yml"
    assert payload["dashboard"]["enable_write_endpoints"] is False
    assert "curl" in payload["sut"]["healthcheck"]["command"]


def test_static_path_traversal_is_blocked(server: dict) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(server["url"] + "/static/../server.py", timeout=2)
    assert exc.value.code == 404


def test_task_spec_endpoint_returns_markdown(server: dict) -> None:
    work_item_id = _seed_work_item(server["paths"])
    with _wait(server["url"] + f"/api/tasks/{work_item_id}/spec") as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    assert payload["work_item_id"] == work_item_id
    assert payload["spec_path"].startswith(".agentic-os/task-specs/")
    assert "Order negative validation" in payload["markdown"]


def test_unknown_task_spec_returns_404(server: dict) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(server["url"] + "/api/tasks/TASK-99999999-000000-missing/spec", timeout=2)
    assert exc.value.code == 404


def test_work_items_list_includes_last_artifact(tmp_path: Path) -> None:
    paths, conn = _runtime(tmp_path)
    try:
        events = EventLog(conn, paths)
        detail = create_work_item_from_payload(
            conn,
            paths,
            events,
            {"title": "Last artifact probe"},
            default_sut_root=".",
        )
        rows = list_work_items(conn)
        assert rows[0]["id"] == detail["work_item"]["id"]
        assert rows[0]["last_artifact_kind"] == "spec"
        assert rows[0]["last_artifact_path"] == detail["work_item"]["spec_path"]
    finally:
        conn.close()
