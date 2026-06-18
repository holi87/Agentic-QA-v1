"""Issue #111 — review/final gate manifests must carry full audit context.

Workflow manifests are the durable evidence reviewers consult when
reconstructing a gate decision. They must record every input the gate
actually used: diff path, apply patch path, work item, reviewer output,
patch sha256, evaluated pillars.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

from agentic_os.events import EventLog
from agentic_os.gates import GateResult, write_gate_result
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.patch_builder import _render_unified_diff, build_skeleton_patch
from agentic_os.storage import init_db
from agentic_os.work_items import (
    create_work_item_from_payload,
    register_work_item_artifact,
)
from agentic_os.workflows import run_final_gate, run_review_gate


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog, Orchestrator]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    return conn, paths, events, orch


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / ".gitignore").write_text(
        ".agentic-os/\nreports/\nbugs/\nevidence/\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=repo, check=True)


def _payload() -> dict:
    return {
        "title": "Audit-context work item",
        "spec_path": "specs/audit.md",
        "priority": "P1",
        "sut_root": ".",
        "scenarios": ["audit"],
    }


def _seed_work_item(conn, paths, events) -> str:
    detail = create_work_item_from_payload(conn, paths, events, _payload(), default_sut_root=".")
    return str(detail["work_item"]["id"])


def _write_patch_artifact(conn, paths, events, work_item_id: str) -> Path:
    skeleton = build_skeleton_patch(
        work_item_id=work_item_id,
        title="Audit",
        priority="P1",
        sut_root=".",
        plan_text="### API\n- audit ctx\n",
    )
    patch_text = _render_unified_diff(rel_path=skeleton.target_rel_path, new_body=skeleton.body)
    patch_path = paths.patches_dir / work_item_id / "audit.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(patch_text, encoding="utf-8")
    rel = str(patch_path.resolve().relative_to(paths.repo_root.resolve()))
    register_work_item_artifact(
        conn, paths, events, work_item_id=work_item_id, kind="patch", path=rel
    )
    return Path(rel)


def test_review_gate_manifest_records_full_audit_context(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _init_git_repo(paths.repo_root)
        work_item_id = _seed_work_item(conn, paths, events)
        patch_rel = _write_patch_artifact(conn, paths, events, work_item_id)
        patch_bytes = (paths.repo_root / patch_rel).read_bytes()
        expected_sha = hashlib.sha256(patch_bytes).hexdigest()

        reviewer_path = paths.evidence_dir / "reviewer.txt"
        reviewer_path.parent.mkdir(parents=True, exist_ok=True)
        reviewer_path.write_text(
            "verdict: APPROVE\n"
            "reason: static_checks_passed\n"
            "findings:\n"
            "- OK:1 - no blocking findings\n"
            "READY\n",
            encoding="utf-8",
        )
        reviewer_rel = reviewer_path.relative_to(paths.repo_root)

        result = run_review_gate(
            orch,
            paths,
            events,
            diff_path=patch_rel,
            scope="api",
            reviewer_output_path=reviewer_rel,
            apply_patch_path=patch_rel,
            work_item_id=work_item_id,
        )
        manifest = json.loads(
            (paths.repo_root / result.manifest_path).read_text(encoding="utf-8")
        )

        # Command line is reconstructable.
        cmd = manifest["command"]
        assert "--diff" in cmd and str(patch_rel) in cmd
        assert "--apply-patch" in cmd and cmd[cmd.index("--apply-patch") + 1] == str(patch_rel)
        assert "--reviewer-output" in cmd
        assert "--work-item" in cmd and cmd[cmd.index("--work-item") + 1] == work_item_id
        assert "--scope" in cmd and cmd[cmd.index("--scope") + 1] == "api"

        ctx = manifest["audit_context"]
        assert ctx["scope"] == "api"
        assert ctx["work_item_id"] == work_item_id
        assert ctx["diff_path"] == str(patch_rel)
        assert ctx["apply_patch_path"] == str(patch_rel)
        assert ctx["reviewer_output_path"] == str(reviewer_rel)
        assert ctx["diff_sha256"] == expected_sha
        assert ctx["reviewed_patch_sha256"] == expected_sha
        assert ctx["applied_patch_sha256"] == expected_sha
        assert ctx["apply_attempted"] is True
        assert ctx["apply_succeeded"] is True
        assert ctx["gate_output_path"].endswith(".txt")
    finally:
        conn.close()


def test_review_gate_manifest_records_apply_not_attempted(tmp_path: Path) -> None:
    """Review-only flow (apply_patch_path=None): audit_context shows
    the apply step was not attempted and applied_patch_sha256 is None."""
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _init_git_repo(paths.repo_root)
        work_item_id = _seed_work_item(conn, paths, events)
        patch_rel = _write_patch_artifact(conn, paths, events, work_item_id)

        result = run_review_gate(
            orch,
            paths,
            events,
            diff_path=patch_rel,
            scope="api",
            apply_patch_path=None,
            work_item_id=work_item_id,
        )
        manifest = json.loads(
            (paths.repo_root / result.manifest_path).read_text(encoding="utf-8")
        )
        ctx = manifest["audit_context"]
        assert ctx["apply_attempted"] is False
        assert ctx["apply_succeeded"] is False
        assert ctx["applied_patch_sha256"] is None
        assert ctx["reviewed_patch_sha256"] is None
        assert ctx["work_item_id"] == work_item_id
        assert "--apply-patch" not in manifest["command"]
    finally:
        conn.close()


def test_final_gate_manifest_records_audit_context_and_pillars(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        result = run_final_gate(orch, paths, events, work_item_id="TASK-XYZ")
        manifest = json.loads(
            (paths.repo_root / result.manifest_path).read_text(encoding="utf-8")
        )
        cmd = manifest["command"]
        assert cmd[:3] == ["agentic-os", "run", "final-gate"]
        assert "--work-item" in cmd
        assert cmd[cmd.index("--work-item") + 1] == "TASK-XYZ"

        ctx = manifest["audit_context"]
        assert ctx["work_item_id"] == "TASK-XYZ"
        assert "gate_output_path" in ctx
        # All five pillars from issue #90 must appear in the
        # `pillars_evaluated` list, regardless of pass/fail status.
        # Additional pillars (e.g. work_item_run_attestation from #184)
        # may also appear — historic pillars are a subset.
        assert {
            "required_files",
            "patch_resolution",
            "run_report",
            "bug_evidence",
            "known_bug_policy",
        }.issubset(set(ctx["pillars_evaluated"])), ctx["pillars_evaluated"]
        # Per-pillar status block stays alongside.
        assert set(manifest["pillars"].keys()) == set(ctx["pillars_evaluated"])
    finally:
        conn.close()
