"""Origin / Host guard for dashboard write endpoints (issue #148).

The server binds to 127.0.0.1 so external hosts cannot reach it. The
guard exists to block two browser-mediated attack classes that bypass
the binding:

* DNS rebinding — attacker-controlled DNS name resolves to 127.0.0.1
  in the victim's browser; the request carries `Host: attacker.com`.
* Cross-origin CSRF POST — a malicious tab open in the operator's
  browser submits a form to http://127.0.0.1:<port>/api/...

The first is caught by the Host header check, the second by the
Origin/Referer check. CLI tools (curl, the test client) send no
Origin/Referer and are accepted.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Iterator, Tuple
from urllib.parse import urlsplit

import pytest

from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_config(repo: Path) -> None:
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        """
runtime:
  root: .agentic-os
  timezone: Europe/Warsaw
  max_parallel_tasks: 4
  heartbeat_seconds: 20
  lease_ttl_seconds: 60
  stale_lease_seconds: 90
  shutdown_grace_seconds: 5
  timeouts:
    default_seconds: 60
    docker_seconds: 60
    test_seconds: 60
    model_seconds: 60
    report_seconds: 60
sut:
  root: .
  compose_file: docker-compose.yml
  compose_project_name: agentic-os-sut
  autostart: false
  healthcheck:
    command: ["true"]
    timeout_seconds: 5
    retries: 1
  test_runner: ./run-tests.sh
  install_shim_allowed: false
models:
  planner: {provider: claude, command: ["claude"], role: opus}
  implementer: {provider: claude, command: ["claude"], role: sonnet}
  reviewer: {provider: codex, command: ["codex"], role: codex}
dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: true
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


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Tuple[str, int]]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    _write_config(repo)
    conn = init_db(paths.db)
    orch = Orchestrator(conn, paths, EventLog(conn, paths))
    orch.seed_phases()
    conn.close()
    port = _free_port()
    srv = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    # Wait for the bind to accept connections so the first test does not
    # race the listener.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    try:
        yield ("127.0.0.1", port)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _raw_request(
    method: str,
    host: str,
    port: int,
    path: str,
    headers: dict,
    body: bytes = b"",
    *,
    inject_token: bool = True,
) -> Tuple[int, bytes]:
    """Send a request with full header control (incl. `Host` override).

    `HTTPConnection.request()` does not expose `skip_host`; that flag
    lives on `putrequest()`. We need it so the test can supply a custom
    Host header for the DNS-rebinding test instead of letting
    http.client compute one from the connection target.

    Issue #291 — unsafe methods need the dashboard auth token. We inject it
    by default (unless the header is already supplied or `inject_token` is
    False) so guard tests exercise host/origin behaviour, not the token gate.
    This file builds requests via `putrequest`, bypassing the conftest
    `HTTPConnection.request` shim, so it manages the token itself.
    """
    headers = dict(headers)
    if (
        inject_token
        and method in ("POST", "PUT", "DELETE")
        and not any(k.lower() == "x-agentic-token" for k in headers)
    ):
        headers["X-Agentic-Token"] = os.environ.get(
            "AGENTIC_DASHBOARD_TOKEN", "test-dashboard-token"
        )
    conn = http.client.HTTPConnection(host, port, timeout=2)
    try:
        skip_host = any(k.lower() == "host" for k in headers)
        conn.putrequest(method, path, skip_host=skip_host, skip_accept_encoding=True)
        lower_keys = {k.lower() for k in headers}
        for key, value in headers.items():
            conn.putheader(key, value)
        if method in ("POST", "PUT", "PATCH"):
            if "content-type" not in lower_keys:
                conn.putheader("Content-Type", "application/json")
            if "content-length" not in lower_keys:
                conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _post(
    host: str, port: int, path: str, headers: dict, *, inject_token: bool = True
) -> Tuple[int, bytes]:
    return _raw_request(
        "POST", host, port, path, headers, body=b"{}", inject_token=inject_token
    )


def _decode(payload: bytes) -> dict:
    try:
        return json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


# ---------- guard pass-through ---------------------------------------------


def test_post_without_origin_or_referer_passes_guard(server):
    host, port = server
    status, body = _post(host, port, "/api/tasks", {})
    # The body may be rejected by the endpoint itself (bad JSON, missing
    # fields), but NOT by the security guard.
    assert status != 403, body
    assert _decode(body).get("error") not in {"forbidden_origin", "forbidden_host"}


def test_post_with_same_origin_passes_guard(server):
    host, port = server
    status, body = _post(host, port, "/api/tasks", {"Origin": f"http://{host}:{port}"})
    assert status != 403, body


def test_post_with_localhost_origin_passes_guard(server):
    host, port = server
    status, body = _post(host, port, "/api/tasks", {"Origin": f"http://localhost:{port}"})
    assert status != 403, body


def test_post_with_same_origin_referer_only_passes_guard(server):
    host, port = server
    status, body = _post(
        host, port, "/api/tasks",
        {"Referer": f"http://localhost:{port}/tasks/new"},
    )
    assert status != 403, body


# ---------- guard rejections -----------------------------------------------


def test_post_with_cross_origin_returns_403(server):
    host, port = server
    status, body = _post(host, port, "/api/tasks", {"Origin": "http://evil.example.com"})
    assert status == 403
    assert _decode(body)["error"] == "forbidden_origin"


def test_post_with_cross_origin_referer_returns_403(server):
    host, port = server
    status, body = _post(host, port, "/api/tasks", {"Referer": "http://evil.example.com/x"})
    assert status == 403
    assert _decode(body)["error"] == "forbidden_origin"


def test_post_with_bad_host_header_returns_403(server):
    host, port = server
    status, body = _post(host, port, "/api/tasks", {"Host": f"attacker.com:{port}"})
    assert status == 403
    assert _decode(body)["error"] == "forbidden_host"


def test_post_with_non_http_origin_scheme_returns_403(server):
    host, port = server
    status, body = _post(host, port, "/api/tasks", {"Origin": "file:///etc/passwd"})
    assert status == 403
    assert _decode(body)["error"] == "forbidden_origin"


# ---------- GET unaffected -------------------------------------------------


def test_get_not_blocked_by_cross_origin(server):
    host, port = server
    status, body = _raw_request(
        "GET", host, port, "/healthz", {"Origin": "http://evil.example.com"}
    )
    assert status == 200
    assert _decode(body) == {"ok": True}


def test_get_not_blocked_by_bad_host(server):
    host, port = server
    status, _ = _raw_request("GET", host, port, "/healthz", {"Host": "attacker.com"})
    # The security guard is POST-only, so a bad Host header alone must
    # not turn a GET into a 403.
    assert status != 403


# ---------- issue #291: unsafe-method auth token ---------------------------


@pytest.mark.no_dashboard_token
def test_post_without_token_returns_403(server):
    host, port = server
    # Loopback + same-origin, but no caller identity → rejected.
    status, body = _post(host, port, "/api/tasks", {}, inject_token=False)
    assert status == 403
    assert _decode(body)["error"] == "forbidden_token"


@pytest.mark.no_dashboard_token
def test_post_with_wrong_token_returns_403(server):
    host, port = server
    status, body = _post(
        host, port, "/api/tasks", {"X-Agentic-Token": "not-the-real-token"}
    )
    assert status == 403
    assert _decode(body)["error"] == "forbidden_token"


def test_post_with_valid_token_passes_guard(server):
    host, port = server
    # Default helper injects the server's token → the auth guard passes.
    status, body = _post(host, port, "/api/tasks", {})
    assert status != 403, body
    assert _decode(body).get("error") != "forbidden_token"


@pytest.mark.no_dashboard_token
def test_delete_without_token_returns_403(server):
    host, port = server
    status, body = _raw_request(
        "DELETE", host, port, "/api/tasks/does-not-exist", {}, inject_token=False
    )
    assert status == 403
    assert _decode(body)["error"] == "forbidden_token"
