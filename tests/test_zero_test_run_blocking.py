"""Issue #100 — a runner that exits 0 with `total=0` (or a `last-run.json`
that lists zero collected tests) must not finalize as a green run.
The only allowed exception is an explicit
`discovery_only: true` / `dry_run: true` flag in the report.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import textwrap
from pathlib import Path

from agentic_os.events import EventLog
from agentic_os.gates import find_run_report_violations
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.workflows import _zero_test_report_status

REPO_ROOT = Path(__file__).resolve().parent.parent


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    return conn, paths, events


def _write_last_run(paths: RuntimePaths, payload: dict) -> None:
    reports = paths.repo_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "last-run.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    (reports / "summary.md").write_text("# stub\n", encoding="utf-8")


def test_zero_test_status_flags_missing_discovery(tmp_path: Path) -> None:
    conn, paths, _events = _runtime(tmp_path)
    try:
        _write_last_run(
            paths,
            {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "failures": []},
        )
        zero, discovery = _zero_test_report_status(paths)
        assert zero is True
        assert discovery is False
    finally:
        conn.close()


def test_zero_test_status_respects_discovery_only(tmp_path: Path) -> None:
    conn, paths, _events = _runtime(tmp_path)
    try:
        _write_last_run(
            paths,
            {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "discovery_only": True,
                "failures": [],
            },
        )
        zero, discovery = _zero_test_report_status(paths)
        assert zero is True
        assert discovery is True
    finally:
        conn.close()


def test_zero_test_status_accepts_dry_run_alias(tmp_path: Path) -> None:
    conn, paths, _events = _runtime(tmp_path)
    try:
        _write_last_run(
            paths,
            {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "dry_run": True,
                "failures": [],
            },
        )
        zero, discovery = _zero_test_report_status(paths)
        assert zero is True
        assert discovery is True
    finally:
        conn.close()


def test_non_boolean_discovery_marker_does_not_bypass_block(tmp_path: Path) -> None:
    """Codex review on #129 — `discovery_only` must be the boolean
    `true`, not a truthy string like `"false"` or a number. Anything
    else is treated as the marker being absent."""
    conn, paths, _events = _runtime(tmp_path)
    try:
        for bad in ("false", "true", 1, 0, "yes", "no", None):
            payload = {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "failures": [],
            }
            if bad is not None:
                payload["discovery_only"] = bad
            _write_last_run(paths, payload)
            zero, discovery = _zero_test_report_status(paths)
            assert zero is True, payload
            assert discovery is False, (
                f"non-boolean discovery_only={bad!r} must not be honored"
            )
            findings = find_run_report_violations(paths)
            assert any(
                "zero tests collected" in f.message for f in findings
            ), payload
    finally:
        conn.close()


def test_positive_total_is_not_zero_test(tmp_path: Path) -> None:
    conn, paths, _events = _runtime(tmp_path)
    try:
        _write_last_run(
            paths,
            {"total": 3, "passed": 3, "failed": 0, "skipped": 0, "failures": []},
        )
        zero, _discovery = _zero_test_report_status(paths)
        assert zero is False
    finally:
        conn.close()


def test_final_gate_run_report_rejects_zero_tests_without_marker(tmp_path: Path) -> None:
    conn, paths, _events = _runtime(tmp_path)
    try:
        _write_last_run(
            paths,
            {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "failures": []},
        )
        findings = find_run_report_violations(paths)
        assert any(
            "zero tests collected" in f.message for f in findings
        ), findings
    finally:
        conn.close()


def _install_zero_test_runner(repo: Path, *, discovery_only: bool = False) -> None:
    """Install run-tests.sh + report scripts that produce a `total=0`
    last-run.json and exit 0 from the runner."""
    import os
    import textwrap

    def _write_executable(path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        os.chmod(path, 0o755)

    cfg = repo / ".qualitycat" / "agentic-os.yml"
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
        ),
        encoding="utf-8",
    )
    payload = {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "failures": [],
    }
    if discovery_only:
        payload["discovery_only"] = True
    _write_executable(
        repo / "scripts" / "copy-reports.sh",
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p reports\n",
    )
    _write_executable(
        repo / "scripts" / "extract-last-run.sh",
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p reports\n"
        "cat > reports/last-run.json <<'JSON'\n"
        + json.dumps(payload, indent=2)
        + "\nJSON\n",
    )
    _write_executable(
        repo / "scripts" / "build-summary.sh",
        "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p reports\n"
        "printf '# stub\\n' > reports/summary.md\n",
    )
    _write_executable(
        repo / "run-tests.sh",
        "#!/usr/bin/env bash\nset -euo pipefail\necho 'no tests'\nexit 0\n",
    )


def test_run_tests_zero_test_run_is_infra_exit_2(tmp_path: Path) -> None:
    """Acceptance criterion 1 — runner exits 0 but reports zero tests:
    `run_tests` must promote the result to infra exit 2 so the run
    cannot pass as green."""
    from agentic_os.orchestrator import Orchestrator
    from agentic_os.workflows import run_tests

    conn, paths, events = _runtime(tmp_path)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    try:
        _install_zero_test_runner(paths.repo_root, discovery_only=False)
        result = run_tests(orch, paths, events)
        assert result.ok is False
        assert result.exit_code == 2
        assert result.failure_kind == "infra"
        event_kinds = [e["kind"] for e in events.tail(200)]
        assert "reports.zero_tests_collected" in event_kinds
    finally:
        conn.close()


def test_run_tests_zero_test_with_discovery_only_stays_green(tmp_path: Path) -> None:
    """Acceptance criterion 3 — an intentional discovery/dry-run with
    `discovery_only: true` must still finalize as exit 0."""
    from agentic_os.orchestrator import Orchestrator
    from agentic_os.workflows import run_tests

    conn, paths, events = _runtime(tmp_path)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    try:
        _install_zero_test_runner(paths.repo_root, discovery_only=True)
        result = run_tests(orch, paths, events)
        assert result.exit_code == 0, result
        assert result.ok is True
        event_kinds = [e["kind"] for e in events.tail(200)]
        assert "reports.zero_tests_collected" not in event_kinds
    finally:
        conn.close()


def test_run_tests_cleans_stale_report_sources_before_current_run(
    tmp_path: Path,
) -> None:
    """A Playwright-only run must not inherit stale JUnit XML from an
    earlier execution. The final report should describe the current run only."""
    from agentic_os.orchestrator import Orchestrator
    from agentic_os.workflows import run_tests

    conn, paths, events = _runtime(tmp_path)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    repo = paths.repo_root
    try:
        _install_zero_test_runner(repo, discovery_only=False)
        for name in ("copy-reports.sh", "extract-last-run.sh", "build-summary.sh"):
            src = REPO_ROOT / "scripts" / name
            dst = repo / "scripts" / name
            shutil.copy(src, dst)
            os.chmod(dst, 0o755)

        stale_junit = repo / "build" / "test-results" / "test" / "TEST-stale.xml"
        stale_junit.parent.mkdir(parents=True, exist_ok=True)
        stale_junit.write_text(
            '<testsuite name="stale" tests="7" failures="0" skipped="0"/>',
            encoding="utf-8",
        )
        (repo / "run-tests.sh").write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                cat > playwright-report.json <<'JSON'
                {
                  "stats": {
                    "expected": 1,
                    "unexpected": 0,
                    "flaky": 0,
                    "skipped": 0
                  },
                  "suites": []
                }
                JSON
                """
            ),
            encoding="utf-8",
        )
        os.chmod(repo / "run-tests.sh", 0o755)

        result = run_tests(orch, paths, events)
        last_run = json.loads((repo / "reports" / "last-run.json").read_text(encoding="utf-8"))

        assert result.exit_code == 0, result
        assert last_run["total"] == 1
        assert last_run["passed"] == 1
        assert not stale_junit.exists()
        event_kinds = [e["kind"] for e in events.tail(200)]
        assert "reports.source_artifacts_cleaned" in event_kinds
    finally:
        conn.close()


def test_final_gate_run_report_accepts_zero_tests_with_discovery_only(
    tmp_path: Path,
) -> None:
    conn, paths, _events = _runtime(tmp_path)
    try:
        _write_last_run(
            paths,
            {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "discovery_only": True,
                "failures": [],
            },
        )
        findings = find_run_report_violations(paths)
        assert not any(
            "zero tests collected" in f.message for f in findings
        ), findings
    finally:
        conn.close()
