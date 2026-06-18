from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from agentic_os import qualitycat
from agentic_os.errors import InfraError, UsageError
from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


def _runtime(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    orch = Orchestrator(conn, paths, EventLog(conn, paths))
    orch.seed_phases()
    events = EventLog(conn, paths)
    return conn, paths, events


def _install_fake_new_bug_script(repo: Path) -> None:
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    target = scripts / "new-bug.sh"
    target.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            BUGS_DIR="${PROJECT_ROOT:-$PWD}/bugs"
            mkdir -p "$BUGS_DIR"
            if [[ "${1:-}" == "--reindex" ]]; then
              exit 0
            fi
            title="${1:-untitled}"
            slug=$(echo "$title" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')
            last=0
            for f in "$BUGS_DIR"/BUG-*.md; do
              [[ -f "$f" ]] || continue
              n=$(basename "$f" | sed -nE 's/^BUG-([0-9]{3})-.*\\.md$/\\1/p')
              [[ -z "$n" ]] && continue
              if (( 10#$n > last )); then last=$((10#$n)); fi
            done
            next=$(printf "BUG-%03d" $((last + 1)))
            echo "# ${next} ${title}" > "$BUGS_DIR/${next}-${slug}.md"
            echo "${next}-${slug}"
            """
        ),
        encoding="utf-8",
    )
    os.chmod(target, 0o755)


def test_file_bug_creates_markdown_evidence_and_db_row(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        _install_fake_new_bug_script(paths.repo_root)
        evidence_src = tmp_path / "screenshot.png"
        evidence_src.write_bytes(b"\x89PNG\x00fake")
        result = qualitycat.file_bug(
            paths=paths,
            events=events,
            conn=conn,
            title="negative quantity accepted",
            severity="P1",
            scenario_tag="@negative @critical @functional-orders",
            evidence_files=[evidence_src],
        )
        assert result.number == 1
        assert result.bug_md_path.exists()
        assert (result.evidence_dir / "screenshot.png").read_bytes() == b"\x89PNG\x00fake"
        row = conn.execute(
            "SELECT severity, status, scenario_tag, evidence_dir FROM bugs;"
        ).fetchone()
        assert row is not None
        assert row["severity"] == "P1"
        assert row["status"] == "open"
        assert row["scenario_tag"].startswith("@negative")
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events;").fetchall()]
        assert "bug.filed" in kinds
        assert "qualitycat.script_run" in kinds
    finally:
        conn.close()


def test_file_bug_rejects_invalid_severity(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        _install_fake_new_bug_script(paths.repo_root)
        with pytest.raises(UsageError):
            qualitycat.file_bug(
                paths=paths,
                events=events,
                conn=conn,
                title="x",
                severity="Critical",
                scenario_tag="@x",
            )
    finally:
        conn.close()


def test_file_bug_increments_number(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        _install_fake_new_bug_script(paths.repo_root)
        first = qualitycat.file_bug(
            paths=paths, events=events, conn=conn,
            title="first", severity="P2", scenario_tag="@first",
        )
        second = qualitycat.file_bug(
            paths=paths, events=events, conn=conn,
            title="second", severity="P3", scenario_tag="@second",
        )
        assert first.number == 1
        assert second.number == 2
    finally:
        conn.close()


def test_missing_script_raises_infra_error(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        # do NOT install the fake script
        with pytest.raises(InfraError, match="helper script missing"):
            qualitycat.file_bug(
                paths=paths, events=events, conn=conn,
                title="x", severity="P1", scenario_tag="@x",
            )
    finally:
        conn.close()
