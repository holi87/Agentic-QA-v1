"""SUT analysis, candidate classification, test planning, and dashboard action gating."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentic_os.analysis import (
    CANDIDATE_BUCKETS,
    analyze_work_item,
)
from agentic_os.events import EventLog
from agentic_os.errors import UsageError
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from agentic_os.storage.db import connect
from agentic_os.test_planning import plan_work_item
from agentic_os.work_items import (
    create_work_item_from_payload,
    get_work_item,
    list_work_item_artifacts,
)
from tests.test_dashboard_task_ui import _DEFAULT_CONFIG, _free_port, _wait


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


def _seed_payload() -> dict:
    return {
        "title": "Order negative validation",
        "priority": "P1",
        "business_goal": "Cover order creation invalid paths.",
        "expected_behavior": "POST /orders rejects invalid payloads with 422.",
        "in_scope": "API validation; error shape; exact-spec evidence.",
        "out_of_scope": "Payment provider sandbox.",
        "known_bugs": "BUG-001 returns 500 instead of 422.",
        "relevant_surfaces": "POST /orders, /checkout page",
        "test_data": "Local non-production fixtures only.",
        "time_budget": "60 minutes",
    }


def _seed_work_item(paths: RuntimePaths, payload: dict | None = None) -> tuple[sqlite3.Connection, str]:
    conn = connect(paths.db)
    events = EventLog(conn, paths)
    detail = create_work_item_from_payload(
        conn,
        paths,
        events,
        payload or _seed_payload(),
        default_sut_root=".",
    )
    return conn, detail["work_item"]["id"]


def test_analyze_creates_all_analysis_artifacts(tmp_path: Path) -> None:
    paths, _seed_conn = _runtime(tmp_path)
    _seed_conn.close()
    conn, work_item_id = _seed_work_item(paths)
    try:
        events = EventLog(conn, paths)
        result = analyze_work_item(conn, paths, events, work_item_id=work_item_id)
        kinds = sorted({a["kind"] for a in result["artifacts"]})
        assert "sut_map" in kinds
        assert "analysis" in kinds
        analysis_dir = paths.runtime_root / "analysis" / work_item_id
        for name in (
            "sut-map.json",
            "requirements.md",
            "risk-map.md",
            "candidate-tests.md",
            "candidate-tests.json",
        ):
            assert (analysis_dir / name).exists(), name
        updated = get_work_item(conn, work_item_id)
        assert updated is not None
        assert updated["status"] == "analyzing"
        registered = list_work_item_artifacts(conn, work_item_id)
        registered_paths = {a["path"] for a in registered}
        for name in (
            "sut-map.json",
            "requirements.md",
            "risk-map.md",
            "candidate-tests.md",
            "candidate-tests.json",
        ):
            assert any(p.endswith(name) for p in registered_paths), name
    finally:
        conn.close()


def test_analyze_classifies_six_candidate_buckets(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_work_item(paths)
    try:
        events = EventLog(conn, paths)
        analyze_work_item(conn, paths, events, work_item_id=work_item_id)
        candidates_md = (paths.runtime_root / "analysis" / work_item_id / "candidate-tests.md").read_text(encoding="utf-8")
        candidates_json = json.loads(
            (paths.runtime_root / "analysis" / work_item_id / "candidate-tests.json").read_text(encoding="utf-8")
        )
        for bucket in CANDIDATE_BUCKETS:
            assert f"## {bucket}" in candidates_md, bucket
        assert candidates_json["items"], "expected structured candidates for planning"
        assert candidates_json["summary"]["Structured candidates"] == len(candidates_json["items"])
    finally:
        conn.close()


def test_analyze_rejects_unknown_task(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn = connect(paths.db)
    try:
        events = EventLog(conn, paths)
        with pytest.raises(UsageError):
            analyze_work_item(conn, paths, events, work_item_id="TASK-19990101-000000-missing")
    finally:
        conn.close()


def test_plan_requires_analysis(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_work_item(paths)
    try:
        events = EventLog(conn, paths)
        with pytest.raises(UsageError):
            plan_work_item(conn, paths, events, work_item_id=work_item_id)
    finally:
        conn.close()


def test_plan_creates_test_plan_after_analysis(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_work_item(paths)
    try:
        events = EventLog(conn, paths)
        analyze_work_item(conn, paths, events, work_item_id=work_item_id)
        result = plan_work_item(conn, paths, events, work_item_id=work_item_id)
        plan_path = paths.runtime_root / "plans" / work_item_id / "TEST-PLAN.md"
        assert plan_path.exists()
        body = plan_path.read_text(encoding="utf-8")
        assert "# TEST-PLAN" in body
        assert work_item_id in body
        assert "Candidate tests" in body
        assert result["plan_path"].endswith("TEST-PLAN.md")
        updated = get_work_item(conn, work_item_id)
        assert updated is not None
        assert updated["status"] == "planned"
        registered = list_work_item_artifacts(conn, work_item_id)
        assert any(a["kind"] == "test_plan" for a in registered)
    finally:
        conn.close()


def test_dashboard_actions_unlocked_by_autonomy_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full-autonomy session must implicitly unlock task action endpoints even
    when dashboard.enable_write_endpoints=false (issue #42)."""
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_work_item(paths)
    conn.close()
    import agentic_os.server as server_module

    monkeypatch.setattr(server_module, "_autonomy_writes_active", lambda: True)

    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()
        # /api/config reports the OR-of-all effective state.
        with urllib.request.urlopen(base + "/api/config", timeout=4) as resp:
            cfg = json.loads(resp.read().decode("utf-8"))
        assert cfg["dashboard"]["enable_write_endpoints"] is True
        assert cfg["dashboard"]["autonomy_unlocks_writes"] is True
        # POST /api/tasks/{id}/analyze must succeed despite config flag being false.
        analyze_req = urllib.request.Request(
            base + f"/api/tasks/{work_item_id}/analyze",
            data=b"",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(analyze_req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["status"] == "analyzing"
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_dashboard_actions_blocked_when_write_disabled(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_work_item(paths)
    conn.close()
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/api/tasks/{work_item_id}/analyze"
        # warm up
        _wait(f"http://127.0.0.1:{port}/healthz", timeout=5).read()
        req = urllib.request.Request(url, data=b"", method="POST",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 403
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_dashboard_actions_run_when_write_enabled(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path, enable_write=True)
    seed.close()
    conn, work_item_id = _seed_work_item(paths)
    conn.close()
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()
        analyze_req = urllib.request.Request(
            base + f"/api/tasks/{work_item_id}/analyze",
            data=b"",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(analyze_req, timeout=4) as resp:
            analyze_body = json.loads(resp.read().decode("utf-8"))
        assert analyze_body["status"] == "analyzing"
        assert len(analyze_body["artifacts"]) == 5

        plan_req = urllib.request.Request(
            base + f"/api/tasks/{work_item_id}/plan",
            data=b"",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(plan_req, timeout=4) as resp:
            plan_body = json.loads(resp.read().decode("utf-8"))
        assert plan_body["status"] == "planned"
        assert plan_body["plan_path"].endswith("TEST-PLAN.md")

        with urllib.request.urlopen(base + f"/api/tasks/{work_item_id}/candidates", timeout=4) as resp:
            candidates_body = json.loads(resp.read().decode("utf-8"))
        assert candidates_body["items"]
        candidate_id = candidates_body["items"][0]["candidate_id"]
        approve_req = urllib.request.Request(
            base + f"/api/tasks/{work_item_id}/candidates/{candidate_id}/approve",
            data=json.dumps({"reason": "dashboard test"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(approve_req, timeout=4) as resp:
            approve_body = json.loads(resp.read().decode("utf-8"))
        assert approve_body["decision"] == "generate_now"
        assert approve_body["summary"]["generate_now"] == 1
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_dashboard_approve_all_candidates_approves_runnable_items(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path, enable_write=True)
    seed.close()
    conn, work_item_id = _seed_work_item(paths)
    try:
        events = EventLog(conn, paths)
        analyze_work_item(conn, paths, events, work_item_id=work_item_id)
        plan_work_item(conn, paths, events, work_item_id=work_item_id)
    finally:
        conn.close()

    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/healthz", timeout=5).read()
        req = urllib.request.Request(
            base + f"/api/tasks/{work_item_id}/candidates/approve-all",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["approved"] >= 1
        assert body["failed"] == 0
        assert body["summary"]["generate_now"] == body["approved"]
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_analyze_respects_disabled_api_surface(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    cfg = paths.repo_root / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        _DEFAULT_CONFIG.format(write="false").replace(
            "  install_shim_allowed: false\n",
            "  install_shim_allowed: false\n"
            "  mode: online\n"
            "  web:\n"
            "    enabled: true\n"
            "    url: https://quality-blog.eu\n"
            "  api:\n"
            "    enabled: false\n",
        ).lstrip(),
        encoding="utf-8",
    )
    conn, work_item_id = _seed_work_item(
        paths,
        {
            "title": "Online blog sweep",
            "priority": "P1",
            "business_goal": "Explore public blog UI.",
            "expected_behavior": "GET /rss and GET /sitemap are mentioned, but API is disabled. The homepage should render.",
            "relevant_surfaces": "https://quality-blog.eu/, /rss.xml, /sitemap.xml",
        },
    )
    try:
        events = EventLog(conn, paths)
        analyze_work_item(conn, paths, events, work_item_id=work_item_id)
        payload = json.loads(
            (
                paths.runtime_root
                / "analysis"
                / work_item_id
                / "candidate-tests.json"
            ).read_text(encoding="utf-8")
        )
        assert payload["items"], "expected UI candidates"
        assert {item["test_type"] for item in payload["items"]} == {"ui"}
        assert all(not item["candidate_id"].startswith("API-") for item in payload["items"])
    finally:
        conn.close()


def test_analyze_does_not_convert_api_only_routes_to_ui_candidates(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_work_item(
        paths,
        {
            "title": "Orders API contract",
            "priority": "P1",
            "business_goal": "Cover order creation invalid paths.",
            "expected_behavior": "POST /orders rejects invalid payloads with 422.",
            "in_scope": "API validation and exact error shape.",
            "relevant_surfaces": "POST /orders",
        },
    )
    try:
        events = EventLog(conn, paths)
        analyze_work_item(conn, paths, events, work_item_id=work_item_id)
        payload = json.loads(
            (
                paths.runtime_root
                / "analysis"
                / work_item_id
                / "candidate-tests.json"
            ).read_text(encoding="utf-8")
        )
        assert payload["items"], "expected API candidate"
        assert {item["test_type"] for item in payload["items"]} == {"api"}
        assert all(not item["candidate_id"].startswith("UI-") for item in payload["items"])
    finally:
        conn.close()


def test_analyze_preserves_absolute_api_url_paths(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_work_item(
        paths,
        {
            "title": "Orders API absolute URL",
            "priority": "P2",
            "business_goal": "Cover the public API endpoint.",
            "expected_behavior": "GET https://api.example.test/api/orders must return HTTP 200.",
            "in_scope": "API contract only.",
            "relevant_surfaces": "GET https://api.example.test/api/orders",
        },
    )
    try:
        events = EventLog(conn, paths)
        analyze_work_item(conn, paths, events, work_item_id=work_item_id)
        payload = json.loads(
            (
                paths.runtime_root
                / "analysis"
                / work_item_id
                / "candidate-tests.json"
            ).read_text(encoding="utf-8")
        )
        api_items = [item for item in payload["items"] if item["candidate_id"].startswith("API-")]
        assert api_items
        assert api_items[0]["target_method"] == "GET"
        assert api_items[0]["target_path"] == "/api/orders"
        assert all(not item["candidate_id"].startswith("UI-") for item in payload["items"])
    finally:
        conn.close()
