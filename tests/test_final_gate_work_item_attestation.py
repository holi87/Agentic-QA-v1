"""Issue #184 — final-gate --work-item must reject when no run-tests run
is attested for that work item.

The audit demonstrated that `run final-gate --work-item X` could pass even
when `run run-tests --work-item X` was never invoked: the gate only
inspected the global `reports/last-run.json`, accepting whatever stale
report happened to sit on disk.

These tests pin down the correct behavior:

- When `--work-item` is supplied AND no `kind='run'` artifact exists for
  that work item, the final gate must REJECT with a clear pillar finding.
- When the work item has at least one registered run artifact, the new
  attestation pillar must not block.
- When `--work-item` is not supplied, the new pillar must remain silent
  so unscoped final-gate behavior is preserved.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.work_items import (
    create_work_item_from_payload,
    register_work_item_artifact,
)
from agentic_os.workflows import run_final_gate


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog, Orchestrator]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    return conn, paths, events, orch


def _seed_work_item(conn, paths, events) -> str:
    detail = create_work_item_from_payload(
        conn,
        paths,
        events,
        {
            "title": "Attestation work item",
            "spec_path": "specs/attestation.md",
            "priority": "P1",
            "sut_root": ".",
            "scenarios": ["smoke"],
        },
        default_sut_root=".",
    )
    return str(detail["work_item"]["id"])


def _write_passing_global_report(paths: RuntimePaths) -> None:
    """A green-looking but unrelated `reports/last-run.json` on disk."""

    reports = paths.repo_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "last-run.json").write_text(
        '{"total": 3, "passed": 3, "failed": 0, "skipped": 0, "failures": []}',
        encoding="utf-8",
    )
    (reports / "summary.md").write_text("ok\n", encoding="utf-8")


def test_final_gate_rejects_when_work_item_has_no_run_artifact(tmp_path: Path) -> None:
    """A stale global report must NOT carry a work-item-scoped final gate.

    Reproduces the audit scenario from issue #184: no `run-tests --work-item X`
    was ever executed, but `reports/last-run.json` is green. Final gate
    must reject with a pillar finding that names the missing attestation.
    """

    conn, paths, events, orch = _runtime(tmp_path)
    try:
        work_item_id = _seed_work_item(conn, paths, events)
        _write_passing_global_report(paths)

        result = run_final_gate(orch, paths, events, work_item_id=work_item_id)

        import json

        manifest = json.loads(
            (paths.repo_root / result.manifest_path).read_text(encoding="utf-8")
        )
        gate = manifest["gate"]
        assert gate["verdict"] == "REJECT", (
            f"final-gate must reject without a work-item run attestation, "
            f"got {gate!r}"
        )
        pillars = manifest["pillars"]
        assert "work_item_run_attestation" in pillars, (
            f"missing work_item_run_attestation pillar; pillars={pillars!r}"
        )
        assert pillars["work_item_run_attestation"]["status"] == "failed"
    finally:
        conn.close()


def test_final_gate_accepts_when_work_item_has_recorded_run(tmp_path: Path) -> None:
    """When the work item has at least one registered run artifact, the
    new attestation pillar must not flag anything.

    Other pillars may still reject (e.g. patch_resolution), but the
    attestation pillar itself must report status='ok'.
    """

    conn, paths, events, orch = _runtime(tmp_path)
    try:
        work_item_id = _seed_work_item(conn, paths, events)
        _write_passing_global_report(paths)

        # Pretend a run-tests workflow attached its manifest as an artifact.
        evidence_dir = paths.evidence_dir / "RUN-FAKE-01"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = evidence_dir / "manifest.json"
        manifest_path.write_text(
            '{"kind": "run-tests", "audit_context": {'
            f'"work_item_id": "{work_item_id}"'
            '}}',
            encoding="utf-8",
        )
        rel = str(manifest_path.resolve().relative_to(paths.repo_root.resolve()))
        register_work_item_artifact(
            conn, paths, events, work_item_id=work_item_id, kind="run", path=rel
        )

        result = run_final_gate(orch, paths, events, work_item_id=work_item_id)

        import json

        manifest = json.loads(
            (paths.repo_root / result.manifest_path).read_text(encoding="utf-8")
        )
        pillars = manifest["pillars"]
        assert pillars["work_item_run_attestation"]["status"] == "ok", (
            f"attestation pillar should pass when a run artifact exists; "
            f"pillars={pillars!r}"
        )
    finally:
        conn.close()


def test_final_gate_rejects_when_only_non_run_tests_artifacts_attested(
    tmp_path: Path,
) -> None:
    """A `kind='run'` artifact pointing at a non-run-tests manifest must
    not satisfy attestation.

    Bypass scenario: `_attach_run_artifacts_to_work_item` records every
    workflow's run with the literal artifact kind `'run'` — including
    `final-gate` itself. Without checking the manifest payload, the
    pillar would accept a `final-gate` self-registered artifact as
    evidence of a real `run-tests` execution.
    """

    conn, paths, events, orch = _runtime(tmp_path)
    try:
        work_item_id = _seed_work_item(conn, paths, events)
        _write_passing_global_report(paths)

        # Register an artifact whose manifest says it is a `final-gate`
        # run, not `run-tests` — exactly what `run_final_gate` writes
        # when invoked with --work-item without a prior run-tests step.
        evidence_dir = paths.evidence_dir / "RUN-FINAL-01"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = evidence_dir / "manifest.json"
        manifest_path.write_text(
            '{"kind": "final-gate", "audit_context": {'
            f'"work_item_id": "{work_item_id}"'
            '}}',
            encoding="utf-8",
        )
        rel = str(manifest_path.resolve().relative_to(paths.repo_root.resolve()))
        register_work_item_artifact(
            conn, paths, events, work_item_id=work_item_id, kind="run", path=rel
        )

        result = run_final_gate(orch, paths, events, work_item_id=work_item_id)

        import json

        manifest = json.loads(
            (paths.repo_root / result.manifest_path).read_text(encoding="utf-8")
        )
        pillars = manifest["pillars"]
        assert pillars["work_item_run_attestation"]["status"] == "failed", (
            f"attestation pillar must NOT accept a non-run-tests manifest; "
            f"pillars={pillars!r}"
        )
        assert manifest["gate"]["verdict"] == "REJECT"
    finally:
        conn.close()


def test_final_gate_without_work_item_skips_attestation(tmp_path: Path) -> None:
    """Unscoped final-gate must keep its existing behavior — the new
    pillar reports `skipped` (not `failed`) so the gate can still
    APPROVE on the green report alone.
    """

    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _write_passing_global_report(paths)

        result = run_final_gate(orch, paths, events, work_item_id=None)

        import json

        manifest = json.loads(
            (paths.repo_root / result.manifest_path).read_text(encoding="utf-8")
        )
        pillars = manifest["pillars"]
        assert "work_item_run_attestation" in pillars
        assert pillars["work_item_run_attestation"]["status"] == "skipped"
    finally:
        conn.close()
