"""Issue #85 — auto-filed product bug Markdown must contain real triage
data: non-TBD severity, scenario/test identity, evidence links, expected
and actual sections, and reproducible repro context.
"""
from __future__ import annotations

import os
import sqlite3
import textwrap
from pathlib import Path

from agentic_os import qualitycat
from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    orch = Orchestrator(conn, paths, EventLog(conn, paths))
    orch.seed_phases()
    return conn, paths, EventLog(conn, paths)


def _install_real_skeleton_script(repo: Path) -> None:
    """Install a fake `new-bug.sh` that writes a frontmatter + sections
    skeleton matching the real script. The body uses TBD placeholders
    so hydration's substitutions are observable in the resulting file.
    """
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
            cat > "$BUGS_DIR/${next}-${slug}.md" <<EOF
---
id: ${next}
title: ${title}
severity: TBD
component: TBD
test: TBD
scenario: TBD
found_by: TBD
status: OPEN
---

# ${next}: ${title}

## Steps to Reproduce

1. TBD

Repro command:
\\`\\`\\`bash
TBD
\\`\\`\\`

## Expected (per spec)

Spec source: TBD

\\`\\`\\`
TBD
\\`\\`\\`

## Actual

\\`\\`\\`
TBD
\\`\\`\\`

## Evidence

- evidence/${next}/TBD

## Impact

TBD
EOF
            """
        ),
        encoding="utf-8",
    )
    os.chmod(target, 0o755)


def test_file_bug_hydrates_frontmatter_with_real_triage_data(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        _install_real_skeleton_script(paths.repo_root)
        filed = qualitycat.file_bug(
            paths=paths,
            events=events,
            conn=conn,
            title="negative quantity accepted",
            severity="P1",
            scenario_tag="@functional-orders",
            test_id="src.tests.api.OrdersSpec",
            error_message="AssertionError: expected 422 got 200",
            actual="AssertionError: expected 422 got 200\n  at OrdersSpec",
            repro_command="./run-tests.sh --tags @functional-orders",
            spec_source="docs/specs/orders.md",
        )
        body = filed.bug_md_path.read_text(encoding="utf-8")
        # Frontmatter is hydrated, not TBD.
        assert "severity: High" in body  # P1 → High
        assert "severity: TBD" not in body
        assert "scenario: @functional-orders" in body
        assert "scenario: TBD" not in body
        assert "test: src.tests.api.OrdersSpec" in body
        assert "test: TBD" not in body
        assert "found_by: agentic-os auto-triage" in body
        assert "found_by: TBD" not in body
    finally:
        conn.close()


def test_file_bug_hydrates_body_sections_with_repro_and_evidence(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        _install_real_skeleton_script(paths.repo_root)
        # Place a trace artifact so file_bug copies it into evidence dir.
        screenshots = paths.repo_root / "reports" / "screens"
        screenshots.mkdir(parents=True, exist_ok=True)
        trace = screenshots / "negative-quantity.png"
        trace.write_bytes(b"PNG-stub")

        filed = qualitycat.file_bug(
            paths=paths,
            events=events,
            conn=conn,
            title="negative quantity accepted",
            severity="P0",
            scenario_tag="@functional-orders",
            evidence_files=[trace],
            test_id="OrdersSpec",
            error_message="expected 422 got 200",
            actual="expected 422 got 200\n  at OrdersSpec",
            repro_command="./run-tests.sh --tags @functional-orders",
            spec_source="docs/specs/orders.md",
        )
        body = filed.bug_md_path.read_text(encoding="utf-8")

        # Steps to Reproduce now names the scenario tag and a concrete command.
        assert "./run-tests.sh --tags @functional-orders" in body
        assert "Scenario tag: `@functional-orders`" in body
        assert "Test id: `OrdersSpec`" in body
        # Expected section names the spec source and is no longer TBD.
        assert "Spec source: docs/specs/orders.md" in body
        assert "Spec source: TBD" not in body
        # Actual section carries the real error message.
        assert "expected 422 got 200" in body
        # Evidence section points at the copied artifact, not TBD.
        rel_evidence = str(
            (filed.evidence_dir / "negative-quantity.png").relative_to(paths.repo_root)
        )
        assert rel_evidence in body
        assert "- evidence/" + filed.bug_id + "/TBD" not in body
        # P0 → Critical label in frontmatter.
        assert "severity: Critical" in body
    finally:
        conn.close()


def test_file_bug_without_extra_metadata_still_produces_no_tbd_body(tmp_path: Path) -> None:
    """Caller may supply only the mandatory fields; hydration must
    still scrub the TBD body sections with sensible defaults."""
    conn, paths, events = _runtime(tmp_path)
    try:
        _install_real_skeleton_script(paths.repo_root)
        filed = qualitycat.file_bug(
            paths=paths,
            events=events,
            conn=conn,
            title="minimal triage",
            severity="P2",
            scenario_tag="@smoke",
        )
        body = filed.bug_md_path.read_text(encoding="utf-8")
        assert "severity: Medium" in body
        assert "scenario: @smoke" in body
        # The Expected/Actual/Steps sections no longer contain a bare
        # `TBD` paragraph — hydration writes a default body.
        assert "1. Reproduce by running the scenario" in body
        assert "Scenario tag: `@smoke`" in body
        assert "Scenario must satisfy its asserted contract." in body
        assert "(no triage detail captured)" in body
    finally:
        conn.close()
