from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentic_os.events import EventLog
from agentic_os.orchestrator import CURRENT_PHASE_ID, Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from agentic_os.storage.db import connect
from agentic_os.work_items import create_work_item_from_payload


def _runtime(tmp_path: Path, *, enable_write: bool = False) -> RuntimePaths:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    _write_config(repo, enable_write=enable_write)
    conn = init_db(paths.db)
    orch = Orchestrator(conn, paths, EventLog(conn, paths))
    orch.seed_phases()
    conn.close()
    return paths


def _write_config(repo: Path, *, enable_write: bool) -> None:
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        f"""
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
  # Issue #187 — dashboard fixtures don't ship a docker-compose.yml, so
  # `autostart=true` would (correctly) abort `run-tests` with infra
  # exit 2 before the dashboard endpoint contract under test runs. Keep
  # autostart off here; lifecycle behaviour is covered by
  # `test_run_persistence_and_sut_lifecycle.py`.
  autostart: false
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
  enable_write_endpoints: {str(enable_write).lower()}
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
""".lstrip(),
        encoding="utf-8",
    )


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def server(tmp_path: Path):
    paths = _runtime(tmp_path)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    import threading

    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    srv._shutdown_requested = True  # type: ignore[attr-defined]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


@pytest.fixture
def writable_server(tmp_path: Path):
    paths = _runtime(tmp_path, enable_write=True)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    import threading

    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
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


def test_status_endpoint_reports_seeded_phases(server: str) -> None:
    payload = _get_json(server + "/api/status")
    assert payload["runtime"] in {"ready", "degraded", "blocked"}
    assert payload["current_phase"] == CURRENT_PHASE_ID
    assert payload["db"] == "ok"
    phase_ids = {p["id"] for p in payload["phases"]}
    assert "05-sonnet-dashboard-runner" in phase_ids
    assert "07-sonnet-qualitycat-integration" in phase_ids
    assert "11-sonnet-dashboard-task-ui" in phase_ids
    assert "14-sonnet-dashboard-run-e2e" in phase_ids


def test_index_html_is_served(server: str) -> None:
    with urllib.request.urlopen(server + "/", timeout=2) as resp:
        body = resp.read().decode("utf-8")
        ctype = resp.headers.get("Content-Type", "")
    assert "text/html" in ctype
    assert "<title>Agentic OS — dashboard</title>" in body


def test_unknown_task_returns_404(server: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(server + "/api/task/does-not-exist", timeout=2)
    assert exc.value.code == 404


def test_tasks_endpoint_lists_work_items(server: str) -> None:
    payload = _get_json(server + "/api/tasks")
    assert payload == {"tasks": [], "orphans": 0}


def test_task_post_is_blocked_when_dashboard_writes_disabled(server: str) -> None:
    req = urllib.request.Request(
        server + "/api/tasks",
        data=json.dumps({"title": "Blocked"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=2)
    assert exc.value.code == 403


def test_task_post_creates_work_item_when_enabled(writable_server: str) -> None:
    req = urllib.request.Request(
        writable_server + "/api/tasks",
        data=json.dumps(
            {
                "title": "Dashboard intake smoke",
                "priority": "P1",
                "business_goal": "Cover dashboard task creation.",
                "expected_behavior": "A task spec is persisted.",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 201
    work_item = payload["work_item"]
    assert work_item["id"].startswith("TASK-")
    assert work_item["status"] == "queued"
    listed = _get_json(writable_server + "/api/tasks")
    assert listed["tasks"][0]["id"] == work_item["id"]
    detail = _get_json(writable_server + "/api/tasks/" + work_item["id"])
    assert detail["artifacts"][0]["kind"] == "spec"
    artifacts = _get_json(writable_server + "/api/tasks/" + work_item["id"] + "/artifacts")
    assert artifacts["artifacts"][0]["path"] == work_item["spec_path"]


def test_task_detail_reports_generated_specs_from_patch_manifest(tmp_path: Path) -> None:
    paths = _runtime(tmp_path, enable_write=True)
    conn = connect(paths.db)
    try:
        detail = create_work_item_from_payload(
            conn,
            paths,
            EventLog(conn, paths),
            {"title": "Generated specs detail"},
        )
    finally:
        conn.close()
    wid = detail["work_item"]["id"]
    manifest_dir = paths.patches_dir / wid / "RUN-1"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "candidate_id": "C-001",
                        "relative_path": "tests/ui/c-001-login.spec.ts",
                        "runner": "playwright-ts",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    import threading

    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        payload = _get_json(f"http://127.0.0.1:{port}/api/tasks/{wid}")
        assert payload["generated_tests"] == [
            {
                "candidate_id": "C-001",
                "relative_path": "tests/ui/c-001-login.spec.ts",
                "runner": "playwright-ts",
                "manifest_path": f".agentic-os/patches/{wid}/RUN-1/manifest.json",
            }
        ]
    finally:
        srv._shutdown_requested = True  # type: ignore[attr-defined]
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_config_save_uses_canonical_config_path(tmp_path: Path) -> None:
    """POST /api/config must never resurrect the legacy `.qualitycat/`
    directory — when the loaded config has no `source`, the fallback target
    is `config/agentic-os.yml` (issue #53)."""
    import threading
    from agentic_os.server import make_server

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    # Write the config at the canonical location only — no `.qualitycat/`.
    cfg_dir = repo / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.joinpath("agentic-os.yml").write_text(
        _DEFAULT_CONFIG_BODY.format(write="true").lstrip(), encoding="utf-8"
    )
    conn = init_db(paths.db)
    Orchestrator(conn, paths, EventLog(conn, paths)).seed_phases()
    conn.close()

    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        # POST the validated, fully-populated config back unchanged (yaml round-trip).
        import yaml as _yaml
        cfg_payload = _yaml.safe_load(
            (repo / "config" / "agentic-os.yml").read_text(encoding="utf-8")
        )
        req = urllib.request.Request(
            base + "/api/config",
            data=json.dumps(cfg_payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        assert result["ok"] is True
        assert result["source"] == "config/agentic-os.yml"
        assert (repo / "config" / "agentic-os.yml").is_file()
        # Legacy path must not be resurrected.
        assert not (repo / ".qualitycat").exists()
    finally:
        srv._shutdown_requested = True
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


# Minimal config body used by the canonical-path test. Keeps the test
# self-contained so the legacy fixture above can stay untouched.
_DEFAULT_CONFIG_BODY = """
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
  mode: local
  compose_file: docker-compose.yml
  compose_project_name: agentic-os-sut
  # Issue #187 — see note on the other config above. The dashboard
  # fixture doesn't seed a real compose file; `autostart=false` keeps
  # the runner-level tests focused on endpoint behaviour.
  autostart: false
  healthcheck:
    command: ["curl", "-fsS", "http://127.0.0.1:3000/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  install_shim_allowed: false
  web:
    enabled: true
    url: http://127.0.0.1:3000
  api:
    enabled: true
    url: http://127.0.0.1:3000/api
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
  enable_write_endpoints: {write}
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


# --- Issue #291: /files/ path-traversal regression -----------------------


def _status_code(url: str, headers: dict | None = None) -> int:
    """GET `url`, returning the HTTP status (treating 4xx as a status, not an
    exception). Retries briefly so the threaded server has time to bind."""
    deadline = time.monotonic() + 5
    last_err: Exception | None = None
    req = urllib.request.Request(url, headers=headers or {})
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(req, timeout=1) as resp:
                return resp.getcode()
        except urllib.error.HTTPError as exc:
            return exc.code
        except (urllib.error.URLError, ConnectionRefusedError) as exc:
            last_err = exc
            time.sleep(0.1)
    raise AssertionError(f"server not reachable at {url}: {last_err}")


def _files_server(tmp_path: Path):
    """Spin a dashboard server and return (base_url, repo_root, teardown)."""
    import threading

    paths = _runtime(tmp_path)
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    def teardown() -> None:
        srv._shutdown_requested = True  # type: ignore[attr-defined]
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)

    return f"http://127.0.0.1:{port}", paths.repo_root, teardown


def test_dashboard_token_file_created_0600_and_reused(tmp_path: Path, monkeypatch) -> None:
    """Issue #291 — generated token persists at 0600 and is reused."""
    import stat as _stat

    from agentic_os.routes.dashboard_server import _load_or_create_dashboard_token

    monkeypatch.delenv("AGENTIC_DASHBOARD_TOKEN", raising=False)
    paths = _runtime(tmp_path)
    token_file = paths.runtime_root / ".dashboard_token"

    first = _load_or_create_dashboard_token(paths)
    assert first and token_file.exists()
    mode = _stat.S_IMODE(token_file.stat().st_mode)
    assert mode == 0o600, oct(mode)
    # Second call adopts the persisted token instead of minting a new one.
    assert _load_or_create_dashboard_token(paths) == first


def test_inject_token_escapes_unsafe_operator_token() -> None:
    """Issue #291 review — an operator token with `"`/`</script>` must not
    break out of the meta attribute or the JS string literal."""
    from agentic_os.routes.dashboard_server import _Handler

    class _H(_Handler):
        dashboard_token = 'abc"</script><b>x'

        def __init__(self) -> None:  # bypass BaseHTTPRequestHandler.__init__
            pass

    html = _H()._inject_dashboard_token("<html><head>\n</head><body></body></html>")
    # No raw breakout of the script element or the attribute.
    assert "</script><b>" not in html
    assert 'content="abc"' not in html
    # The escaped forms are present instead.
    assert "\\u003c/script\\u003e" in html
    assert "&lt;/script&gt;" in html or "&quot;" in html


def test_served_html_embeds_dashboard_token(tmp_path: Path) -> None:
    """Issue #291 — the browser UI must receive the token to authenticate."""
    base, _repo_root, teardown = _files_server(tmp_path)
    try:
        with urllib.request.urlopen(base + "/", timeout=2) as resp:
            body = resp.read().decode("utf-8")
        assert 'name="agentic-dashboard-token"' in body
        assert "X-Agentic-Token" in body  # the fetch shim wires the header
    finally:
        teardown()


def test_files_serves_whitelisted_report(tmp_path: Path) -> None:
    base, repo_root, teardown = _files_server(tmp_path)
    try:
        report = repo_root / "reports" / "summary.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# ok\n", encoding="utf-8")
        assert _status_code(base + "/files/reports/summary.md") == 200
    finally:
        teardown()


def test_files_rejects_dotdot_traversal(tmp_path: Path) -> None:
    base, repo_root, teardown = _files_server(tmp_path)
    try:
        # A real secret one level above repo_root the traversal would target.
        (repo_root.parent / "secret.txt").write_text("top-secret\n", encoding="utf-8")
        for payload in (
            "/files/../secret.txt",
            "/files/reports/../../secret.txt",
        ):
            assert _status_code(base + payload) == 404, payload
    finally:
        teardown()


def test_files_rejects_absolute_path(tmp_path: Path) -> None:
    base, _repo_root, teardown = _files_server(tmp_path)
    try:
        assert _status_code(base + "/files//etc/passwd") == 404
        assert _status_code(base + "/files/%2Fetc%2Fpasswd") == 404
    finally:
        teardown()


def test_files_rejects_symlink_escape(tmp_path: Path) -> None:
    base, repo_root, teardown = _files_server(tmp_path)
    try:
        outside = repo_root.parent / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "loot.txt").write_text("loot\n", encoding="utf-8")
        link = repo_root / "reports" / "escape"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            import pytest

            pytest.skip("symlinks unsupported on this platform")
        # `escape` resolves outside repo_root → must not be served.
        assert _status_code(base + "/files/reports/escape/loot.txt") == 404
    finally:
        teardown()


def test_files_rejects_non_whitelisted_repo_path(tmp_path: Path) -> None:
    base, repo_root, teardown = _files_server(tmp_path)
    try:
        # Under repo_root but outside the served whitelist (config dir).
        cfg = repo_root / ".qualitycat" / "agentic-os.yml"
        assert cfg.exists()
        assert _status_code(base + "/files/.qualitycat/agentic-os.yml") == 404
    finally:
        teardown()
