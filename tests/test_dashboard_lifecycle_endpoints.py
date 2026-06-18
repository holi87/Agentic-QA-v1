"""Dashboard lifecycle, git, agent, skill, suggestion, and write-unlock endpoints."""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server, set_full_mode_override
from agentic_os.storage import init_db


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
  compose_file: null
  compose_project_name: phase2-sut
  autostart: false
  healthcheck:
    command: ["true"]
    timeout_seconds: 1
    retries: 0
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
  triager:
    provider: antigravity
    command: ["agy", "--model", "gemini-3.1-pro-high"]
    role: gemini
    auto_fire: false
dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: {enable_write}
paths:
  reports: reports
  bugs: bugs
  evidence: evidence
  prompts: config/prompts
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


def _make_runtime(tmp_path: Path, *, enable_write: bool) -> RuntimePaths:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_BASE_CFG.format(enable_write=str(enable_write).lower()).lstrip(), encoding="utf-8")
    conn = init_db(paths.db)
    Orchestrator(conn, paths, EventLog(conn, paths)).seed_phases()
    conn.close()
    return paths


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
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionRefusedError) as exc:
            last = exc
            time.sleep(0.1)
    raise AssertionError(f"unreachable: {last}")


def _post_json(url: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError:
            data = {"raw": body_text}
        return exc.code, data


@pytest.fixture(autouse=True)
def _reset_full_mode():
    set_full_mode_override(False)
    yield
    set_full_mode_override(False)


def test_serve_agents_lists_four_roles(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=False)
    srv, thread, base = _serve(paths)
    try:
        data = _get_json(base + "/api/agents")
        roles = {a["role"] for a in data["agents"]}
        assert roles == {"planner", "implementer", "reviewer", "triager"}
        triager = next(a for a in data["agents"] if a["role"] == "triager")
        assert triager["provider"] == "antigravity"
        assert triager["command"][0] == "agy"
    finally:
        _shutdown(srv, thread)


def test_sut_lifecycle_endpoints_blocked_when_writes_off(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=False)
    srv, thread, base = _serve(paths)
    try:
        code, body = _post_json(base + "/api/sut/start")
        assert code == 403
        assert body["error"] == "dashboard_write_disabled"
    finally:
        _shutdown(srv, thread)


def test_sut_lifecycle_endpoints_work_when_writes_on(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=True)
    srv, thread, base = _serve(paths)
    try:
        # compose_file=null -> sut-start is a no-op success.
        code, body = _post_json(base + "/api/sut/start")
        assert code == 200
        assert body["ok"] is True
        # healthcheck with "true" command passes immediately.
        code, body = _post_json(base + "/api/sut/healthcheck")
        assert code == 200
        assert body["ok"] is True
    finally:
        _shutdown(srv, thread)


def test_full_mode_override_unlocks_write_endpoints(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=False)
    set_full_mode_override(True)
    srv, thread, base = _serve(paths)
    try:
        code, body = _post_json(base + "/api/sut/start")
        assert code == 200
        assert body["ok"] is True
        # GET /api/config exposes full_mode flag.
        cfg = _get_json(base + "/api/config")
        assert cfg["dashboard"]["full_mode"] is True
        assert cfg["dashboard"]["enable_write_endpoints"] is True
        preflight = _get_json(base + "/api/dashboard/preflight")
        write_check = next(
            c for c in preflight["checks"] if c["id"] == "dashboard_write_mode"
        )
        assert write_check["status"] == "pass"
    finally:
        _shutdown(srv, thread)
        set_full_mode_override(False)


def test_runtime_recover_endpoint(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=True)
    srv, thread, base = _serve(paths)
    try:
        code, body = _post_json(base + "/api/runtime/recover")
        assert code == 200
        assert body["ok"] is True
        assert body["task_id"]
    finally:
        _shutdown(srv, thread)


def test_sut_git_status_uninitialized(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=False)
    srv, thread, base = _serve(paths)
    try:
        data = _get_json(base + "/api/sut/git/status")
        # repo / sut.root = '.' so paths.repo_root which has no .git in tmp.
        assert data["initialized"] is False
    finally:
        _shutdown(srv, thread)


def test_sut_git_status_reports_head_sha_for_initialized_repo(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=False)
    (paths.repo_root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=paths.repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=paths.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=paths.repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "README.md"], cwd=paths.repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=paths.repo_root,
        check=True,
        capture_output=True,
    )
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=paths.repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    srv, thread, base = _serve(paths)
    try:
        data = _get_json(base + "/api/sut/git/status")
        assert data["initialized"] is True
        assert data["head_sha"] == expected
    finally:
        _shutdown(srv, thread)


def test_sut_git_init_creates_repo(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=True)
    srv, thread, base = _serve(paths)
    try:
        code, body = _post_json(base + "/api/sut/git/init")
        assert code == 200
        assert body["ok"] is True
        # Re-init reports already_initialized.
        code, body2 = _post_json(base + "/api/sut/git/init")
        assert code == 200
        assert body2["detail"].get("already_initialized") is True
    finally:
        _shutdown(srv, thread)


def test_sut_git_remote_validates_url(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=True)
    srv, thread, base = _serve(paths)
    try:
        _post_json(base + "/api/sut/git/init")
        code, body = _post_json(base + "/api/sut/git/remote", {"url": "ftp://evil.example/repo"})
        assert code == 400
        assert "remote URL" in body.get("message", "")
    finally:
        _shutdown(srv, thread)


def test_skills_endpoint_lists_migrated(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=False)
    # Set up minimal skills/ in the runtime repo so /api/skills sees something.
    skill = paths.repo_root / "skills" / "claude" / "example.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: example\ndescription: hi\n---\nBody.\n", encoding="utf-8")
    srv, thread, base = _serve(paths)
    try:
        data = _get_json(base + "/api/skills")
        ids = {s["id"] for s in data["skills"]}
        assert "claude/example" in ids
    finally:
        _shutdown(srv, thread)


def test_skill_toggle_persists_to_yaml(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=True)
    skill = paths.repo_root / "skills" / "claude" / "x.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: x\ndescription: d\n---\nbody\n", encoding="utf-8")
    srv, thread, base = _serve(paths)
    try:
        code, body = _post_json(base + "/api/skills/claude/x/enable", {"role": "planner"})
        assert code == 200
        assert body["ok"] is True
        # Persisted.
        cfg_path = paths.repo_root / "config" / "skills.yml"
        assert cfg_path.exists()
        import yaml as _yaml

        cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert "claude/x" in cfg["skills"]["per_role"]["planner"]["enabled"]
    finally:
        _shutdown(srv, thread)


def test_suggestions_endpoint_returns_list(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=False)
    srv, thread, base = _serve(paths)
    try:
        data = _get_json(base + "/api/suggestions")
        assert isinstance(data["suggestions"], list)
    finally:
        _shutdown(srv, thread)


def test_agent_update_persists_command(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=True)
    srv, thread, base = _serve(paths)
    try:
        code, body = _post_json(
            base + "/api/agents/triager",
            {"command": ["agy", "--model", "gemini-3.1-pro-low"], "auto_fire": True},
        )
        assert code == 200
        assert body["ok"] is True
        # GET reflects new command.
        agents = _get_json(base + "/api/agents")
        triager = next(a for a in agents["agents"] if a["role"] == "triager")
        assert triager["command"] == ["agy", "--model", "gemini-3.1-pro-low"]
        assert triager["auto_fire"] is True
    finally:
        _shutdown(srv, thread)


def test_agent_test_endpoint_reports_missing_binary(tmp_path: Path) -> None:
    paths = _make_runtime(tmp_path, enable_write=True)
    srv, thread, base = _serve(paths)
    try:
        code, body = _post_json(base + "/api/agents/triager/test")
        assert code == 200
        # agy is unlikely to be on PATH in CI.
        assert body["ok"] is False or body["exit_code"] == 0
    finally:
        _shutdown(srv, thread)
