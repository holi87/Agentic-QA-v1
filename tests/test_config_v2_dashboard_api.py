"""Config v2 schema, secret redaction, and dashboard config API behavior."""
from __future__ import annotations

import json
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentic_os.config import (
    AgenticConfig,
    ConfigError,
    load_config,
    load_or_default,
    redact_secrets,
    write_config,
)
from agentic_os.errors import ConfigError as _ConfigErr
from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db


_LEGACY_CONFIG = """
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


_V2_SUT_EXTRA = """
  kind: web_api
  base_url: http://127.0.0.1:3000
  api_base_url: http://127.0.0.1:3000/api
  ui_url: http://127.0.0.1:3000
  openapi:
    sources:
      - type: file
        value: docs/openapi.yaml
  docs:
    sources:
      - type: file
        value: docs/requirements.md
  credentials:
    ref_type: env
    value: TEST_USER_TOKEN
  tests_dir: tests
  tests:
    api:
      runner: playwright-ts
    ui:
      runner: playwright-ts
"""


def _write_config(repo: Path, *, enable_write: bool = False, sut_extra: str = "") -> Path:
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    body = _LEGACY_CONFIG.format(enable_write=str(enable_write).lower()).lstrip()
    if sut_extra:
        body = body.replace(
            "  install_shim_allowed: false\n",
            "  install_shim_allowed: false\n" + sut_extra,
        )
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_legacy_config_still_loads(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    loaded = load_config(cfg)
    assert loaded.raw["sut"]["root"] == "."
    assert "kind" not in loaded.raw["sut"]


def test_v2_config_with_optional_fields_loads(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, sut_extra=_V2_SUT_EXTRA)
    loaded = load_config(cfg)
    sut = loaded.raw["sut"]
    assert sut["kind"] == "web_api"
    assert sut["api_base_url"] == "http://127.0.0.1:3000/api"
    assert sut["openapi"]["sources"][0]["type"] == "file"
    assert sut["credentials"]["ref_type"] == "env"
    assert sut["tests"]["api"]["runner"] == "playwright-ts"


def test_unknown_key_still_fails(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, sut_extra="  totally_unknown_key: 1\n")
    with pytest.raises(_ConfigErr):
        load_config(cfg)


def test_url_with_unsupported_scheme_fails(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, sut_extra="  base_url: ftp://example.com\n")
    with pytest.raises(_ConfigErr) as exc:
        load_config(cfg)
    assert "scheme" in str(exc.value)


def test_path_traversal_in_openapi_source_fails(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        sut_extra=textwrap.dedent(
            """\
              openapi:
                sources:
                  - type: file
                    value: ../../etc/passwd
            """
        ),
    )
    with pytest.raises(_ConfigErr) as exc:
        load_config(cfg)
    assert "traversal" in str(exc.value)


def test_credentials_env_requires_valid_name(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        sut_extra=textwrap.dedent(
            """\
              credentials:
                ref_type: env
                value: "1bad-name"
            """
        ),
    )
    with pytest.raises(_ConfigErr):
        load_config(cfg)


def test_redact_secrets_masks_env_value() -> None:
    raw = {
        "sut": {
            "credentials": {"ref_type": "env", "value": "TEST_TOKEN"},
        }
    }
    safe = redact_secrets(raw)
    assert safe["sut"]["credentials"]["value"] == "env:TEST_TOKEN"
    # Original is untouched.
    assert raw["sut"]["credentials"]["value"] == "TEST_TOKEN"


def test_write_config_round_trip(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, sut_extra=_V2_SUT_EXTRA)
    loaded = load_config(cfg_path)
    target = tmp_path / "rewritten.yml"
    write_config(target, loaded.raw)
    reloaded = load_config(target)
    assert reloaded.raw["sut"]["api_base_url"] == "http://127.0.0.1:3000/api"


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _make_runtime(tmp_path: Path, *, enable_write: bool) -> RuntimePaths:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    _write_config(repo, enable_write=enable_write, sut_extra=_V2_SUT_EXTRA)
    conn = init_db(paths.db)
    orch = Orchestrator(conn, paths, EventLog(conn, paths))
    orch.seed_phases()
    conn.close()
    return paths


def _serve(tmp_path: Path, *, enable_write: bool):
    paths = _make_runtime(tmp_path, enable_write=enable_write)
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


def test_get_config_redacts_credentials(tmp_path: Path) -> None:
    srv, thread, base = _serve(tmp_path, enable_write=False)
    try:
        payload = _get_json(base + "/api/config")
        creds = payload["sut"]["credentials"]
        # ref_type stays so dashboard can show wiring, value is masked.
        assert creds["ref_type"] == "env"
        assert creds["value"] == "env:TEST_USER_TOKEN"
        assert payload["sut"]["kind"] == "web_api"
    finally:
        _shutdown(srv, thread)


def test_post_config_blocked_when_writes_disabled(tmp_path: Path) -> None:
    srv, thread, base = _serve(tmp_path, enable_write=False)
    try:
        req = urllib.request.Request(
            base + "/api/config",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 403
    finally:
        _shutdown(srv, thread)


def test_post_config_writes_when_enabled(tmp_path: Path) -> None:
    srv, thread, base = _serve(tmp_path, enable_write=True)
    try:
        # Read the test repo's config directly — GET /api/config returns a
        # filtered/redacted view, not the full raw needed for a round-trip POST.
        raw_cfg = load_or_default(tmp_path / "repo").raw
        raw_cfg["sut"]["tests_dir"] = "tests/changed"
        req = urllib.request.Request(
            base + "/api/config",
            data=json.dumps(raw_cfg).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["ok"] is True
        reloaded = _get_json(base + "/api/config")
        assert reloaded["sut"]["tests_dir"] == "tests/changed"
    finally:
        _shutdown(srv, thread)


def test_post_config_rejects_remote_dashboard_host(tmp_path: Path) -> None:
    srv, thread, base = _serve(tmp_path, enable_write=True)
    try:
        payload = {"dashboard": {"host": "0.0.0.0", "port": 8765, "enable_write_endpoints": True}}
        req = urllib.request.Request(
            base + "/api/config",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 400
    finally:
        _shutdown(srv, thread)


def test_post_sut_mode_persists_online_web_url(tmp_path: Path) -> None:
    srv, thread, base = _serve(tmp_path, enable_write=True)
    try:
        payload = {
            "mode": "online",
            "web": {"enabled": True, "url": "https://qualitycat.com.pl"},
            "api": {"enabled": False, "url": ""},
        }
        req = urllib.request.Request(
            base + "/api/sut/mode",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["ok"] is True
        reloaded = load_or_default(tmp_path / "repo").raw
        sut = reloaded["sut"]
        assert sut["mode"] == "online"
        assert sut["web"] == {"enabled": True, "url": "https://qualitycat.com.pl"}
        assert sut["api"] == {"enabled": False}
        assert sut["compose_file"] is None
        assert sut["autostart"] is False
    finally:
        _shutdown(srv, thread)
