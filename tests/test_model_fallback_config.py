"""Model fallback-chain config validation and cooldown settings."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agentic_os.config import load_config
from agentic_os.errors import ConfigError


_BASE = textwrap.dedent(
    """
    runtime:
      root: agentic-os-runtime
      timezone: Europe/Warsaw
      max_parallel_tasks: 1
      heartbeat_seconds: 10
      lease_ttl_seconds: 600
      stale_lease_seconds: 1800
      shutdown_grace_seconds: 30
      timeouts:
        default_seconds: 600
        docker_seconds: 120
        test_seconds: 900
        model_seconds: 600
        report_seconds: 120

    sut:
      root: .
      compose_file: docker-compose.yml
      compose_project_name: app
      autostart: false
      healthcheck:
        command: ["sh", "-c", "exit 0"]
        timeout_seconds: 5
        retries: 1
      test_runner: scripts/run-tests.sh
      install_shim_allowed: false

    models:
      planner:
        provider: claude
        command: ["claude", "--model", "opus"]
        role: opus
{planner_extras}
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
      prompts: prompts

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
)


def _write(tmp_path: Path, planner_extras: str = "") -> Path:
    cfg = tmp_path / "agentic-os.yml"
    cfg.write_text(_BASE.format(planner_extras=planner_extras), encoding="utf-8")
    return cfg


def test_fallback_chain_with_valid_entries_passes(tmp_path: Path) -> None:
    extras = (
        '        fallback:\n'
        '          - provider: codex\n'
        '            command: ["codex"]\n'
        '            role: codex\n'
        '          - provider: antigravity\n'
        '            command: ["agy", "--model", "gemini-3.1-pro-high"]\n'
        '            role: gemini\n'
    )
    cfg = load_config(_write(tmp_path, extras))
    chain = cfg.raw["models"]["planner"]["fallback"]
    assert [c["provider"] for c in chain] == ["codex", "antigravity"]


def test_fallback_chain_rejects_duplicate_provider(tmp_path: Path) -> None:
    extras = (
        '        fallback:\n'
        '          - provider: claude\n'
        '            command: ["claude", "--model", "sonnet"]\n'
        '            role: sonnet\n'
    )
    with pytest.raises(ConfigError, match="unique provider"):
        load_config(_write(tmp_path, extras))


def test_fallback_chain_rejects_unknown_provider(tmp_path: Path) -> None:
    extras = (
        '        fallback:\n'
        '          - provider: bogus\n'
        '            command: ["bogus"]\n'
        '            role: codex\n'
    )
    with pytest.raises(ConfigError, match="provider"):
        load_config(_write(tmp_path, extras))


def test_fallback_signals_must_compile_as_regex(tmp_path: Path) -> None:
    extras = '        fallback_signals: ["[unterminated"]\n'
    with pytest.raises(ConfigError, match="valid Python regex"):
        load_config(_write(tmp_path, extras))


def test_cooldown_seconds_must_be_non_negative(tmp_path: Path) -> None:
    extras = '        cooldown_seconds: -1\n'
    with pytest.raises(ConfigError, match="cooldown_seconds"):
        load_config(_write(tmp_path, extras))
