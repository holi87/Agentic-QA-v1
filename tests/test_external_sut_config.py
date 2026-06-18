"""External-SUT config validation (Wave 17, issue #356, ADR-0001).

The OS connects to an external SUT (web/API URL + optional DB) and never
starts it, so an `mode: online` config must validate WITHOUT the
Compose-only keys (`compose_file`, `compose_project_name`, `autostart`,
`healthcheck`, `test_runner`, `install_shim_allowed`). Local mode keeps the
strict requirements for backward compatibility.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_os.config import load_config
from agentic_os.errors import ConfigError


# Everything except the `sut:` block — a complete, valid config otherwise.
_NON_SUT = """\
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
  enable_write_endpoints: false
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

# Minimal external SUT — no Compose-lifecycle keys (compose_file,
# compose_project_name, autostart, install_shim_allowed). healthcheck +
# test_runner stay: the OS still probes the SUT and runs the generated tests.
_EXTERNAL_SUT = """\
sut:
  root: .
  mode: online
  healthcheck:
    command: ["curl", "-fsS", "https://staging.example.com/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  web:
    enabled: true
    url: https://staging.example.com
  api:
    enabled: true
    url: https://staging.example.com/api
"""

# Strict local SUT, but missing the Compose keys (must still fail).
_LOCAL_SUT_MISSING_COMPOSE = """\
sut:
  root: .
  mode: local
  healthcheck:
    command: ["curl", "-fsS", "http://127.0.0.1:3000/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  web:
    enabled: true
    url: http://127.0.0.1:3000
"""


def _write(repo: Path, sut_block: str) -> Path:
    cfg = repo / "agentic-os.yml"
    cfg.write_text(_NON_SUT + sut_block, encoding="utf-8")
    return cfg


def test_external_sut_loads_without_compose_keys(tmp_path: Path) -> None:
    loaded = load_config(_write(tmp_path, _EXTERNAL_SUT))
    sut = loaded.raw["sut"]
    assert sut["mode"] == "online"
    assert sut["web"]["url"] == "https://staging.example.com"
    assert "compose_file" not in sut
    assert "compose_project_name" not in sut
    assert "autostart" not in sut
    assert "install_shim_allowed" not in sut


def test_external_sut_requires_at_least_one_endpoint_url(tmp_path: Path) -> None:
    sut = """\
sut:
  root: .
  mode: online
  web:
    enabled: false
  api:
    enabled: false
"""
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, sut))
    assert "url" in str(exc.value).lower()


def test_external_sut_accepts_optional_db_reference(tmp_path: Path) -> None:
    sut = _EXTERNAL_SUT + """\
  db:
    ref_type: env
    value: DATABASE_URL
"""
    loaded = load_config(_write(tmp_path, sut))
    assert loaded.raw["sut"]["db"]["ref_type"] == "env"


def test_external_sut_db_rejects_inline_secret(tmp_path: Path) -> None:
    sut = _EXTERNAL_SUT + """\
  db:
    ref_type: literal
    value: postgres://user:pass@host/db
"""
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, sut))


def test_external_sut_tolerates_legacy_compose_keys(tmp_path: Path) -> None:
    # Migration: an operator who keeps placeholder Compose keys still validates.
    sut = _EXTERNAL_SUT + """\
  compose_file: docker-compose.yml
  compose_project_name: legacy
  autostart: false
  test_runner: ./run-tests.sh
  install_shim_allowed: false
"""
    loaded = load_config(_write(tmp_path, sut))
    assert loaded.raw["sut"]["compose_file"] == "docker-compose.yml"


def test_local_mode_still_requires_compose_keys(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, _LOCAL_SUT_MISSING_COMPOSE))
    assert "compose_file" in str(exc.value) or "missing" in str(exc.value).lower()
