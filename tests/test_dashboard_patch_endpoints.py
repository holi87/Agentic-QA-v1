"""Dashboard patch listing, config exposure, agent update, and abandon-patch endpoints."""
from __future__ import annotations

import json
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
from agentic_os.work_items import (
    create_work_item_from_payload,
    register_work_item_artifact,
)


_BASE_CFG = """
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
  kind: web_api
  base_url: http://127.0.0.1:3000
  api_base_url: http://127.0.0.1:3000/api
  ui_url: http://127.0.0.1:3000
  openapi:
    sources:
      - type: file
        value: docs/openapi.yaml
  credentials:
    ref_type: env
    value: TEST_USER_TOKEN
  tests_dir: tests
  tests:
    api:
      runner: playwright-ts
    ui:
      runner: playwright-ts
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
  enable_write_endpoints: {enable_write}
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


def _build_runtime(tmp_path: Path, *, enable_write: bool) -> tuple[RuntimePaths, str]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_BASE_CFG.format(enable_write=str(enable_write).lower()).lstrip(), encoding="utf-8")
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    detail = create_work_item_from_payload(
        conn,
        paths,
        events,
        {
            "title": "Phase 10 dashboard fixture",
            "business_goal": "Test patches endpoint and abandon flow",
            "expected_behavior": "n/a",
        },
        default_sut_root=".",
    )
    work_item_id = detail["work_item"]["id"]
    patch_file = paths.repo_root / "fixture.patch"
    patch_file.write_text("diff --git a/x b/x\n", encoding="utf-8")
    register_work_item_artifact(
        conn,
        paths,
        events,
        work_item_id=work_item_id,
        kind="patch",
        path="fixture.patch",
    )
    conn.close()
    return paths, work_item_id


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(paths: RuntimePaths):
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread, f"http://127.0.0.1:{port}"


def _shutdown(srv, thread) -> None:
    srv._shutdown_requested = True  # type: ignore[attr-defined]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _get_json(url: str) -> dict:
    deadline = time.monotonic() + 5
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionRefusedError) as exc:
            last_err = exc
            time.sleep(0.1)
    raise AssertionError(f"server not reachable at {url}: {last_err}")


def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_patches_endpoint_lists_waiting_patches(tmp_path: Path) -> None:
    paths, work_item_id = _build_runtime(tmp_path, enable_write=False)
    srv, thread, base = _serve(paths)
    try:
        payload = _get_json(base + "/api/patches")
        patches = payload["patches"]
        assert len(patches) == 1
        assert patches[0]["work_item_id"] == work_item_id
        assert patches[0]["state"] == "waiting"
        assert patches[0]["blocking"] is True
        # Per-task variant must filter.
        per_task = _get_json(base + "/api/patches/" + work_item_id)
        assert len(per_task["patches"]) == 1
    finally:
        _shutdown(srv, thread)


def test_get_config_exposes_v2_fields_to_dashboard(tmp_path: Path) -> None:
    paths, _ = _build_runtime(tmp_path, enable_write=False)
    srv, thread, base = _serve(paths)
    try:
        cfg = _get_json(base + "/api/config")
        sut = cfg["sut"]
        assert sut["kind"] == "web_api"
        assert sut["api_base_url"] == "http://127.0.0.1:3000/api"
        assert sut["openapi"]["sources"][0]["type"] == "file"
        # Credentials must come through redacted.
        assert sut["credentials"]["value"] == "env:TEST_USER_TOKEN"
        assert sut["tests"]["api"]["runner"] == "playwright-ts"
    finally:
        _shutdown(srv, thread)


def test_agent_update_endpoint_blocked_when_writes_disabled(tmp_path: Path) -> None:
    paths, _ = _build_runtime(tmp_path, enable_write=False)
    srv, thread, base = _serve(paths)
    try:
        req = urllib.request.Request(
            base + "/api/agents/planner",
            data=json.dumps(
                {"provider": "codex", "command": ["codex"], "auto_fire": False}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        assert exc.value.code == 403
    finally:
        _shutdown(srv, thread)


def test_agent_update_endpoint_persists_command_and_allows_triager_reload(tmp_path: Path) -> None:
    paths, _ = _build_runtime(tmp_path, enable_write=True)
    srv, thread, base = _serve(paths)
    try:
        body = _post_json(
            base + "/api/agents/triager",
            {
                "provider": "antigravity",
                "command": ["agy", "--model", "gemini-3.1-pro-high"],
                "auto_fire": True,
            },
        )
        assert body == {"ok": True, "role": "triager"}
        agents = _get_json(base + "/api/agents")["agents"]
        triager = next(a for a in agents if a["role"] == "triager")
        assert triager["provider"] == "antigravity"
        assert triager["command"] == ["agy", "--model", "gemini-3.1-pro-high"]
        assert triager["auto_fire"] is True
    finally:
        _shutdown(srv, thread)


def test_abandon_patch_endpoint_blocked_when_writes_disabled(tmp_path: Path) -> None:
    paths, work_item_id = _build_runtime(tmp_path, enable_write=False)
    srv, thread, base = _serve(paths)
    try:
        req = urllib.request.Request(
            base + "/api/tasks/" + work_item_id + "/abandon-patch",
            data=json.dumps({"patch_path": "fixture.patch", "reason": "test"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        assert exc.value.code == 403
    finally:
        _shutdown(srv, thread)


def test_abandon_patch_endpoint_works_when_writes_enabled(tmp_path: Path) -> None:
    paths, work_item_id = _build_runtime(tmp_path, enable_write=True)
    srv, thread, base = _serve(paths)
    try:
        req = urllib.request.Request(
            base + "/api/tasks/" + work_item_id + "/abandon-patch",
            data=json.dumps(
                {"patch_path": "fixture.patch", "reason": "operator test abandon"}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["ok"] is True
        assert body["result"]["decision_id"]
        # Patch state flips to "abandoned" — final gate now passes.
        patches = _get_json(base + "/api/patches")["patches"]
        assert patches[0]["state"] == "abandoned"
        assert patches[0]["blocking"] is False
    finally:
        _shutdown(srv, thread)


def test_abandon_patch_requires_patch_path_and_reason(tmp_path: Path) -> None:
    paths, work_item_id = _build_runtime(tmp_path, enable_write=True)
    srv, thread, base = _serve(paths)
    try:
        req = urllib.request.Request(
            base + "/api/tasks/" + work_item_id + "/abandon-patch",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        assert exc.value.code == 400
    finally:
        _shutdown(srv, thread)
