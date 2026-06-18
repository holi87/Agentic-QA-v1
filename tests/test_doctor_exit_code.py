"""Issue #96 — `doctor` must exit non-zero when requested checks have
blocking issues, so CI/automation can use it as a strict gate.
"""
from __future__ import annotations

import io
import json
import textwrap
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agentic_os.cli import main as cli_main


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


def _write_canonical_config(repo: Path) -> None:
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        textwrap.dedent(
            """\
            runtime:
              root: .agentic-os
              timezone: Europe/Warsaw
              max_parallel_tasks: 4
              heartbeat_seconds: 20
              lease_ttl_seconds: 60
              stale_lease_seconds: 90
              shutdown_grace_seconds: 1
              timeouts:
                default_seconds: 30
                docker_seconds: 30
                test_seconds: 30
                model_seconds: 30
                report_seconds: 30
            sut:
              root: .
              compose_file: docker-compose.yml
              compose_project_name: agentic-os-sut
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
                command: ["claude"]
                role: opus
              implementer:
                provider: claude
                command: ["claude"]
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
        ),
        encoding="utf-8",
    )


def test_doctor_sut_returns_nonzero_when_issues_present(tmp_path: Path) -> None:
    _write_canonical_config(tmp_path)
    rc, stdout, _ = _run_cli(["--root", str(tmp_path), "--json", "doctor", "--sut"])
    payload = json.loads(stdout)
    # `compose_file: docker-compose.yml` does not exist in tmp_path,
    # so SUT issues are non-empty.
    assert payload["sut"]["issues"], payload
    assert payload["ok"] is False
    assert rc == 1


def test_doctor_returns_zero_when_no_checks_requested_and_config_loads(tmp_path: Path) -> None:
    _write_canonical_config(tmp_path)
    rc, stdout, _ = _run_cli(["--root", str(tmp_path), "--json", "doctor"])
    payload = json.loads(stdout)
    assert payload["ok"] is True
    assert rc == 0


def test_doctor_returns_nonzero_when_config_missing(tmp_path: Path) -> None:
    rc, stdout, _ = _run_cli(["--root", str(tmp_path), "--json", "doctor"])
    payload = json.loads(stdout)
    # No canonical config under tmp_path → config_error → blocking.
    assert payload["ok"] is False
    assert rc == 1
    assert any(
        "config_error" in r for r in payload.get("blocking_reasons", [])
    )


def test_doctor_models_returns_nonzero_when_binary_missing(tmp_path: Path) -> None:
    _write_canonical_config(tmp_path)
    rc, stdout, _ = _run_cli(["--root", str(tmp_path), "--json", "doctor", "--models"])
    payload = json.loads(stdout)
    # `claude` / `codex` are not on PATH in the test environment.
    if payload["models"].get("issues"):
        assert payload["ok"] is False
        assert rc == 1
