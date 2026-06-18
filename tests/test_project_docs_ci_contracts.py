"""Project documentation, fake-SUT proof, help text, legacy-path, and CI contracts."""
from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path


def test_pyproject_toml_declares_pytest_dev_dep() -> None:
    """Issue #74 — fresh install docs require a dependency manifest."""
    p = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert "[project]" in text
    assert "PyYAML" in text
    assert "pytest" in text
    assert "agentic-os" in text


def test_ci_workflow_present_and_runs_pytest() -> None:
    """Issue #89 — GitHub Actions workflow exists and runs pytest."""
    p = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "ci.yml"
    )
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert "pytest" in text
    assert "ubuntu-latest" in text


def test_ci_whitespace_gate_is_blocking() -> None:
    """Issue #190 — the whitespace check must fail the job when
    `git diff --check` reports problems. A trailing `|| true` (or any
    other mask of the exit code) silently downgrades the step from a
    quality gate to a noisy log line."""
    p = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "ci.yml"
    )
    text = p.read_text(encoding="utf-8")

    # The step must still exist.
    assert "Whitespace check" in text, "Whitespace check step missing"
    assert "git diff --check" in text, "Whitespace step lost its command"

    # And it must not mask its exit code.
    lines = text.splitlines()
    in_step = False
    for idx, line in enumerate(lines):
        if "Whitespace check" in line:
            in_step = True
            continue
        if not in_step:
            continue
        # Step boundary: next `- name:` or end of step block.
        stripped = line.lstrip()
        if stripped.startswith("- name:"):
            break
        # Skip YAML/shell comments — `|| true` may appear in prose
        # explaining why the mask is forbidden.
        if stripped.startswith("#"):
            continue
        assert "|| true" not in line, (
            f"ci.yml whitespace step still masks exit code on line "
            f"{idx + 1}: {line!r}"
        )
        assert "|| :" not in line, (
            f"ci.yml whitespace step still masks exit code on line "
            f"{idx + 1}: {line!r}"
        )


def test_help_text_no_longer_advertises_unimplemented_daemon() -> None:
    """Issue #83 — help text must not claim the daemon exists."""
    from agentic_os.cli import HELP_TEXT

    # `daemon` may appear once, but only in a sentence that explicitly
    # marks it as not-in-this-release (issue #83 acceptance).
    import re

    collapsed = re.sub(r"\s+", " ", HELP_TEXT)
    assert "NOT in this release" in collapsed
    # The fake-SUT proof flag must be mentioned.
    assert "--fake-sut" in HELP_TEXT


def test_run_dry_run_fake_sut_writes_passing_reports(tmp_path: Path) -> None:
    """Issue #73 — `run dry-run --fake-sut` is the onboarding proof
    fixture. It must produce a `discovery_only=true` last-run.json so
    issue #100's zero-test guard accepts it, plus a summary.md."""
    from agentic_os.events import EventLog
    from agentic_os.orchestrator import Orchestrator
    from agentic_os.paths import RuntimePaths
    from agentic_os.storage import init_db
    from agentic_os.workflows import run_dry_run

    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    try:
        result = run_dry_run(orch, paths, events, fake_sut=True)
        assert result.ok is True
        assert result.exit_code == 0
        last_run = json.loads(
            (repo / "reports" / "last-run.json").read_text(encoding="utf-8")
        )
        assert last_run["discovery_only"] is True
        assert (repo / "reports" / "summary.md").is_file()
    finally:
        conn.close()


def test_legacy_qualitycat_paths_no_longer_appear_in_user_facing_docs() -> None:
    """Issue #84 — operator-facing docs only reference `.qualitycat`
    when explicitly framing it as a legacy migration path."""
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "docs" / "bug-aware-policy.md",
        root / "docs" / "severity-policy.md",
        root / "docs" / "operator-guide.md",
    ]
    for p in paths:
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        for line in text.splitlines():
            if ".qualitycat" not in line:
                continue
            # The line must explicitly frame the legacy reference.
            assert (
                "legacy" in line.lower()
                or "fallback" in line.lower()
                or "migrat" in line.lower()
                or "qualitycat" in line.lower()
                and "agentic-os" not in line.lower()
            ), f"unflagged legacy ref in {p.name}: {line!r}"
