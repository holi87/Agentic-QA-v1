"""Issue #97 — `runtime.root` from config must drive the runtime path
helper. The default runtime root is visible (`agentic-os-runtime/`) while
legacy `.agentic-os/` remains readable for old checkouts.

Issue #98 — `sut.tests_dir` must reach the v2 generators so generated
specs land in the configured test directory.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path


def _write_config(repo: Path, *, runtime_root: str, tests_dir: str) -> None:
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        textwrap.dedent(
            f"""\
            runtime:
              root: {runtime_root}
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
              compose_file: docker-compose.yml
              compose_project_name: agentic-os-sut
              autostart: false
              tests_dir: {tests_dir}
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


def test_runtime_paths_from_config_honors_runtime_root(tmp_path: Path) -> None:
    from agentic_os.paths import runtime_paths_from_config

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo, runtime_root="custom-state", tests_dir="tests")

    paths = runtime_paths_from_config(repo)
    assert paths.runtime_root == repo / "custom-state"


def test_runtime_paths_from_config_falls_back_when_config_missing(tmp_path: Path) -> None:
    from agentic_os.paths import runtime_paths_from_config

    repo = tmp_path / "repo"
    repo.mkdir()
    # No config — new checkouts use the visible default.
    paths = runtime_paths_from_config(repo)
    assert paths.runtime_root == repo / "agentic-os-runtime"


def test_runtime_paths_from_config_keeps_legacy_runtime_when_only_legacy_exists(tmp_path: Path) -> None:
    from agentic_os.paths import runtime_paths_from_config

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".agentic-os").mkdir()
    paths = runtime_paths_from_config(repo)
    assert paths.runtime_root == repo / ".agentic-os"


def test_open_runtime_uses_configured_visible_runtime_root(tmp_path: Path) -> None:
    from agentic_os.orchestrator import open_runtime

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo, runtime_root="agentic-os-runtime", tests_dir="tests")
    conn, paths, _events, _orch = open_runtime(repo)
    try:
        assert paths.runtime_root == repo / "agentic-os-runtime"
        assert paths.db.exists()
        assert not (repo / ".agentic-os" / "state.db").exists()
    finally:
        conn.close()


def test_generated_specs_land_in_configured_tests_dir(tmp_path: Path) -> None:
    """Issue #98 — when `sut.tests_dir` is non-default, the v2
    generator must place spec files there."""
    from agentic_os.generators.api import generate_api_tests
    from agentic_os.plan_v2 import PlanItem

    item = PlanItem(
        candidate_id="API-CASE-1",
        title="case 1",
        test_type="api",
        priority="P2",
        decision="generate_now",
        expected_assertion="GET /orders must return HTTP 200",
        source_refs=["docs/spec.md#orders/get"],
        target_method="GET",
        target_path="/orders",
        required_test_data="(operator: define minimal test data)",
        cleanup_strategy="read-only endpoint",
        generator_target="playwright-ts",
    )
    specs = list(generate_api_tests([item], tests_dir="qa/integration"))
    assert specs, "expected one API spec"
    assert specs[0].relative_path.startswith("qa/integration/api/"), specs[0].relative_path
