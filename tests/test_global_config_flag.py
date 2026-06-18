"""Issue #77 — the global `--config <path>` flag must be honored by
every command that loads config, not just shown in the diagnostic
banner.
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


def _write_online_config(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        textwrap.dedent(
            """\
            runtime:
              root: .agentic-os
              timezone: Europe/Warsaw
              max_parallel_tasks: 1
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
              mode: online
              compose_file: docker-compose.yml
              compose_project_name: agentic-os-sut
              autostart: false
              web:
                enabled: true
                url: https://example.com
              api:
                enabled: false
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


def test_global_config_flag_changes_doctor_sut_payload(tmp_path: Path) -> None:
    """A `--config <online>` override must make `doctor --sut` report
    `mode=online`, not the absent (or local) canonical config in the
    repo working tree."""
    override = tmp_path / "outside-repo" / "agentic-os.yml"
    _write_online_config(override)
    repo = tmp_path / "repo"
    repo.mkdir()
    rc, stdout, _ = _run_cli([
        "--root", str(repo),
        "--config", str(override),
        "--json", "doctor", "--sut",
    ])
    payload = json.loads(stdout)
    assert payload["config"]["override_active"] is True
    assert payload["sut"]["mode"] == "online", payload
    # Online SUT: no compose file required, so SUT issues should not
    # mention compose_file.
    issues = payload["sut"].get("issues") or []
    assert not any("compose_file" in i for i in issues), issues


def test_global_config_override_is_visible_in_payload(tmp_path: Path) -> None:
    override = tmp_path / "outside-repo" / "agentic-os.yml"
    _write_online_config(override)
    repo = tmp_path / "repo"
    repo.mkdir()
    rc, stdout, _ = _run_cli([
        "--root", str(repo),
        "--config", str(override),
        "--json", "doctor",
    ])
    payload = json.loads(stdout)
    # The source field should reflect the override path even when it
    # lives outside repo_root.
    assert str(override) in payload["config"]["source"], payload


def test_no_override_falls_back_to_canonical_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cfg = repo / "config" / "agentic-os.yml"
    _write_online_config(cfg)
    rc, stdout, _ = _run_cli([
        "--root", str(repo),
        "--json", "doctor", "--sut",
    ])
    payload = json.loads(stdout)
    assert payload["config"]["override_active"] is False
    assert payload["config"]["source"] == "config/agentic-os.yml"
