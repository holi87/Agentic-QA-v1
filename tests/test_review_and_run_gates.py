from __future__ import annotations

import json
import os
import sqlite3
import textwrap
from pathlib import Path

import pytest

from agentic_os.errors import UsageError
from agentic_os.events import EventLog
from agentic_os.gates import GateFinding, GateResult, merge_patch_if_approved, parse_gate_output, static_review_gate
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.security import resolve_repo_path
from agentic_os.storage import init_db
from agentic_os.workflows import run_tests


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog, Orchestrator]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    return conn, paths, events, orch


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    os.chmod(path, 0o755)


def _install_config(repo: Path) -> None:
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


def _install_report_scripts(
    repo: Path,
    *,
    write_reports: bool = True,
    scenario: str = "known bug remains red",
    tags: list[str] | None = None,
) -> None:
    tags = tags or ["@known-bug", "@bug-001", "@functional-orders", "@regression"]
    last_run_payload = {
        "total": 1,
        "passed": 0,
        "failed": 1,
        "skipped": 0,
        "failures": [
            {
                "scenario": scenario,
                "classname": "orders",
                "tags": tags,
                "error_message": "expected exact behavior",
                "slug": scenario.lower().replace(" ", "-"),
                "junit_xml": "build/test-results/test/TEST-fake.xml",
            }
        ],
    }
    if write_reports:
        extract_script = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "mkdir -p reports\n"
            "cat > reports/last-run.json <<'JSON'\n"
            + json.dumps(last_run_payload, indent=2)
            + "\nJSON\n"
        )
        _write_executable(
            repo / "scripts" / "copy-reports.sh",
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            mkdir -p reports
            """,
        )
        _write_executable(
            repo / "scripts" / "extract-last-run.sh",
            extract_script,
        )
        _write_executable(
            repo / "scripts" / "build-summary.sh",
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            mkdir -p reports
            printf '# Test Run Summary\\n\\nKnown-Bug Failures: 1\\n' > reports/summary.md
            """,
        )
    else:
        for name in ("copy-reports.sh", "extract-last-run.sh", "build-summary.sh"):
            _write_executable(
                repo / "scripts" / name,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                mkdir -p reports
                """,
            )


def _install_new_bug_script(repo: Path) -> None:
    _write_executable(
        repo / "scripts" / "new-bug.sh",
        """\
#!/usr/bin/env bash
set -euo pipefail
mkdir -p bugs evidence
if [[ "${1:-}" == "--reindex" ]]; then
  touch bugs/README.md
  exit 0
fi
title="${1:?title required}"
slug="$(echo "$title" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-50)"
file="bugs/BUG-001-${slug:-untitled}.md"
cat > "$file" <<EOF
---
id: BUG-001
title: $title
severity: TBD
status: OPEN
---

# BUG-001: $title
EOF
touch bugs/README.md
""",
    )


def test_parse_gate_output_requires_explicit_verdict() -> None:
    gate = parse_gate_output(
        "verdict: APPROVE\n"
        "reason: static_checks_passed\n"
        "\n"
        "findings:\n"
        "- OK:1 - no blocking findings\n"
        "READY\n"
    )

    assert gate.approved is True
    assert gate.reason == "static_checks_passed"


def test_static_review_gate_rejects_unsafe_subprocess() -> None:
    diff = (
        "diff --git a/app.py b/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,1 +1,1 @@\n"
        "+subprocess.run('rm -rf /', shell=True)\n"
    )

    gate = static_review_gate(diff, scope="api")

    assert gate.verdict == "REJECT"
    assert gate.reason == "unsafe_subprocess"
    assert gate.findings[0].path == "app.py"


def test_reject_blocks_patch_merge_without_touching_worktree(tmp_path: Path) -> None:
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        target = paths.repo_root / "target.txt"
        target.write_text("before\n", encoding="utf-8")
        patch = paths.repo_root / "change.patch"
        patch.write_text(
            textwrap.dedent(
                """\
                diff --git a/target.txt b/target.txt
                --- a/target.txt
                +++ b/target.txt
                @@ -1 +1 @@
                -before
                +after
                """
            ),
            encoding="utf-8",
        )
        gate = GateResult(
            verdict="REJECT",
            reason="assertion_weakened",
            findings=[GateFinding("target.txt", 1, "blocked")],
        )

        result = merge_patch_if_approved(paths=paths, events=events, patch_path=patch, gate=gate)

        assert result.blocked is True
        assert result.applied is False
        assert target.read_text(encoding="utf-8") == "before\n"
    finally:
        conn.close()


def test_run_tests_workflow_keeps_known_bug_red_and_reports_exist(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _install_config(paths.repo_root)
        _install_report_scripts(paths.repo_root, write_reports=True)
        # Issue #110 — `@bug-001` is only credible when the referenced
        # bug record exists. Seed the canonical bug file so triage can
        # resolve the tag to `known_bug_red`.
        bugs_dir = paths.repo_root / "bugs"
        bugs_dir.mkdir(parents=True, exist_ok=True)
        (bugs_dir / "BUG-001-known-bug-remains-red.md").write_text(
            "# BUG-001 — known bug remains red\nstatus: known\n",
            encoding="utf-8",
        )
        _write_executable(
            paths.repo_root / "run-tests.sh",
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            echo 'known bug remains red'
            exit 1
            """,
        )

        result = run_tests(orch, paths, events)

        assert result.ok is False
        assert result.exit_code == 1
        assert result.failure_kind == "product"
        assert result.reports_path == "reports"
        assert result.bugs_opened == []
        assert (paths.repo_root / "reports" / "last-run.json").exists()
        assert (paths.repo_root / "reports" / "summary.md").exists()
        triage = json.loads(
            next((paths.runtime_root / "runs").glob("*/triage.json")).read_text(encoding="utf-8")
        )
        assert triage["summary"]["known_bug_red"] == 1
    finally:
        conn.close()


def test_run_tests_auto_files_product_bug_from_report_triage(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _install_config(paths.repo_root)
        _install_report_scripts(
            paths.repo_root,
            write_reports=True,
            scenario="negative quantity accepted",
            tags=["@functional-orders", "@regression"],
        )
        _install_new_bug_script(paths.repo_root)
        _write_executable(
            paths.repo_root / "run-tests.sh",
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            exit 1
            """,
        )

        result = run_tests(orch, paths, events)

        assert result.exit_code == 1
        assert result.bugs_opened, "product failure should file a bug"
        assert any((paths.repo_root / "bugs").glob("BUG-001-*.md"))
        triage = json.loads(
            next((paths.runtime_root / "runs").glob("*/triage.json")).read_text(encoding="utf-8")
        )
        assert triage["summary"]["product_bug"] == 1
        assert triage["bugs_opened"] == result.bugs_opened
    finally:
        conn.close()


def test_run_tests_failure_without_reports_becomes_infra(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _install_config(paths.repo_root)
        _install_report_scripts(paths.repo_root, write_reports=False)
        _write_executable(
            paths.repo_root / "run-tests.sh",
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            exit 1
            """,
        )

        result = run_tests(orch, paths, events)

        assert result.ok is False
        assert result.exit_code == 2
        assert result.failure_kind == "infra"
        assert result.reports_path is None
    finally:
        conn.close()


def test_repo_path_rejects_traversal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(UsageError, match="escapes repo root"):
        resolve_repo_path(repo, "../outside.sh", label="sut.test_runner")
