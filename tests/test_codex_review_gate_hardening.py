"""Codex review gate and final gate hardening regressions."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agentic_os.events import EventLog
from agentic_os.errors import UsageError
from agentic_os.ids import ulid
from agentic_os.gates import (
    GateFinding,
    GateResult,
    final_gate,
    parse_gate_output,
    static_review_gate,
)
from agentic_os.orchestrator import Orchestrator
from agentic_os.patch_builder import _render_unified_diff, build_skeleton_patch
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.work_items import (
    create_work_item_from_payload,
    list_work_item_artifacts,
    register_work_item_artifact,
    update_work_item_status,
)
from agentic_os.workflows import run_review_gate


def _runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog, Orchestrator]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    return conn, paths, events, orch


def _payload() -> dict[str, str]:
    return {
        "title": "Order negative validation",
        "priority": "P1",
        "business_goal": "Cover invalid order creation.",
        "expected_behavior": "POST /orders rejects invalid payloads with 422.",
        "in_scope": "API validation.",
        "out_of_scope": "Payment provider.",
        "known_bugs": "None.",
        "relevant_surfaces": "POST /orders.",
        "test_data": "Local fixtures.",
        "time_budget": "30 minutes",
    }


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / ".gitignore").write_text(
        ".agentic-os/\nreports/\nbugs/\nevidence/\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=repo, check=True)


def _seed_work_item(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
) -> str:
    detail = create_work_item_from_payload(
        conn,
        paths,
        events,
        _payload(),
        default_sut_root=".",
    )
    return str(detail["work_item"]["id"])


def _write_patch_artifact(
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    events: EventLog,
    work_item_id: str,
) -> tuple[Path, str]:
    skeleton = build_skeleton_patch(
        work_item_id=work_item_id,
        title="Order negative validation",
        priority="P1",
        sut_root=".",
        plan_text="### API\n- POST /orders rejects invalid payloads with 422\n",
    )
    patch_text = _render_unified_diff(
        rel_path=skeleton.target_rel_path,
        new_body=skeleton.body,
    )
    patch_path = paths.patches_dir / work_item_id / "skeleton.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(patch_text, encoding="utf-8")
    rel_patch = str(patch_path.resolve().relative_to(paths.repo_root.resolve()))
    register_work_item_artifact(
        conn,
        paths,
        events,
        work_item_id=work_item_id,
        kind="patch",
        path=rel_patch,
    )
    return Path(rel_patch), skeleton.target_rel_path


def _write_gate_text(paths: RuntimePaths, name: str, gate: GateResult) -> Path:
    target = paths.evidence_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(gate.to_text(), encoding="utf-8")
    return Path(str(target.resolve().relative_to(paths.repo_root.resolve())))


def _install_final_gate_files(paths: RuntimePaths) -> None:
    required = (
        "scripts/agentic-os.sh",
        "scripts/assertion-guard.py",
        "scripts/copy-reports.sh",
        "scripts/extract-last-run.sh",
        "scripts/build-summary.sh",
        "run-tests.sh",
        "config/agentic-os.yml.example",
    )
    for rel in required:
        target = paths.repo_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(paths.repo_root / "run-tests.sh", 0o755)
    # Issue #90 — final gate now requires a finalized run report on disk.
    # Install a passing stub so other tests can focus on their own scenario.
    _install_passing_run_report(paths)


def _install_passing_run_report(paths: RuntimePaths) -> None:
    reports_dir = paths.repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "last-run.json").write_text(
        json.dumps(
            {
                "ran_at": "2026-01-01T00:00:00Z",
                "total": 1,
                "passed": 1,
                "failed": 0,
                "skipped": 0,
                "failures": [],
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "summary.md").write_text("# summary\n\n_no failures_\n", encoding="utf-8")


def test_reject_review_gate_does_not_change_working_tree(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _init_git_repo(paths.repo_root)
        work_item_id = _seed_work_item(conn, paths, events)
        patch_rel, _target_rel = _write_patch_artifact(conn, paths, events, work_item_id)
        reviewer_rel = _write_gate_text(
            paths,
            "reviewer-reject.txt",
            GateResult(
                verdict="REJECT",
                reason="reviewer_rejected",
                findings=[GateFinding(str(patch_rel), 1, "reviewer rejected patch")],
            ),
        )

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

        assert result.ok is False
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=paths.repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        assert status.stdout == ""
        assert subprocess.run(["git", "diff", "--quiet"], cwd=paths.repo_root).returncode == 0

        artifacts = list_work_item_artifacts(conn, work_item_id)
        assert any(a["kind"] == "patch" and a["path"] == str(patch_rel) for a in artifacts)
        gate_artifacts = [a for a in artifacts if a["kind"] == "gate"]
        assert gate_artifacts
        for artifact in gate_artifacts:
            gate = parse_gate_output((paths.repo_root / artifact["path"]).read_text(encoding="utf-8"))
            assert gate.approved is False

        event_kinds = [event["kind"] for event in events.tail(100)]
        assert "gate.patch_blocked" in event_kinds
        assert "gate.patch_applied" not in event_kinds
    finally:
        conn.close()


def test_empty_patch_and_ambiguous_reviewer_output_reject_without_apply(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _init_git_repo(paths.repo_root)
        empty = static_review_gate("", scope="api")
        assert empty.verdict == "REJECT"
        assert empty.reason == "empty_diff"

        with pytest.raises(ValueError, match="READY"):
            parse_gate_output(
                "verdict: APPROVE\n"
                "reason: reviewer_claims_ready\n"
                "findings:\n"
                "- OK:1 - no blocking findings\n"
            )

        work_item_id = _seed_work_item(conn, paths, events)
        patch_rel, target_rel = _write_patch_artifact(conn, paths, events, work_item_id)
        reviewer_rel = _write_gate_text(
            paths,
            "ambiguous-reviewer.txt",
            GateResult(
                verdict="APPROVE",
                reason="will_be_overwritten",
                findings=[],
            ),
        )
        (paths.repo_root / reviewer_rel).write_text(
            "verdict: APPROVE\n"
            "reason: reviewer_claims_ready\n"
            "findings:\n"
            "- OK:1 - no blocking findings\n",
            encoding="utf-8",
        )

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

        assert result.ok is False
        assert not (paths.repo_root / target_rel).exists()
        manifest = json.loads((paths.repo_root / result.manifest_path).read_text(encoding="utf-8"))
        assert manifest["gate"]["reason"] == "ambiguous_reviewer_output"
        event_kinds = [event["kind"] for event in events.tail(100)]
        assert "gate.patch_blocked" in event_kinds
        assert "gate.patch_applied" not in event_kinds
    finally:
        conn.close()


@pytest.mark.parametrize(
    "removed",
    [
        "expect(response.status).toBe(200);",
        "await expect(api.post('/orders')).rejects.toThrow();",
        "response.should.equal(200);",
        "chai.expect(body.total).to.equal(10);",
        "assert.equal(response.status, 200);",
        "assertions.assertThat(response.status).isEqualTo(200);",
        "assert response.status_code == 200 \\",
        "with pytest.raises(ValueError):",
        "Assertions.assertEquals(200, response.statusCode());",
        "assertThat(response.statusCode()).isEqualTo(200);",
        "expect(response).to have_http_status(:ok)",
        "is_expected.to eq(200)",
        ".toEqual({ status: 200 });",
    ],
)
def test_static_gate_rejects_removed_assertions_across_languages(removed: str) -> None:
    diff = (
        "diff --git a/tests/example.spec b/tests/example.spec\n"
        "--- a/tests/example.spec\n"
        "+++ b/tests/example.spec\n"
        "@@ -1 +0,0 @@\n"
        f"-{removed}\n"
    )

    gate = static_review_gate(diff, scope="assertion")

    assert gate.verdict == "REJECT"
    assert gate.reason == "assertion_weakened"
    assert any("removed assertion" in finding.message for finding in gate.findings)


@pytest.mark.parametrize(
    "added",
    [
        "xit('skips a broken test', () => {});",
        "xdescribe('disabled suite', () => {});",
        "test.skip('skips exact spec', () => {});",
        "@pytest.mark.skip(reason='broken')",
        "@pytest.mark.xfail(reason='known flaky')",
    ],
)
def test_static_gate_rejects_skip_without_operator_decision(added: str) -> None:
    diff = (
        "diff --git a/tests/example.spec.ts b/tests/example.spec.ts\n"
        "+++ b/tests/example.spec.ts\n"
        "@@ -0,0 +1 @@\n"
        f"+{added}\n"
    )

    gate = static_review_gate(diff, scope="api")

    assert gate.verdict == "REJECT"
    assert gate.reason == "skip_without_decision"


def test_static_gate_rejects_unpaired_known_bug_tag() -> None:
    diff = (
        "diff --git a/tests/orders.feature b/tests/orders.feature\n"
        "+++ b/tests/orders.feature\n"
        "@@ -0,0 +1 @@\n"
        "+@known-bug @functional-orders @regression\n"
    )

    gate = static_review_gate(diff, scope="api")

    assert gate.verdict == "REJECT"
    assert gate.reason == "known_bug_requires_decision"


def test_assertion_scope_rejects_known_bug_scenario_modification() -> None:
    diff = (
        "diff --git a/tests/orders.feature b/tests/orders.feature\n"
        "--- a/tests/orders.feature\n"
        "+++ b/tests/orders.feature\n"
        "@@ -1,3 +1,3 @@\n"
        " @known-bug @bug-001 @functional-orders @regression\n"
        " Scenario: invalid order remains wrong\n"
        "-When old invalid order is submitted\n"
        "+When invalid order is submitted\n"
    )

    gate = static_review_gate(diff, scope="assertion")

    assert gate.verdict == "REJECT"
    assert gate.reason == "known_bug_requires_decision"
    assert any("known-bug scenario" in finding.message for finding in gate.findings)


def test_final_gate_and_status_transition_require_approved_patch_gate(tmp_path: Path) -> None:
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        _install_final_gate_files(paths)
        work_item_id = _seed_work_item(conn, paths, events)
        patch_rel, _target_rel = _write_patch_artifact(conn, paths, events, work_item_id)

        gate = final_gate(paths)
        assert gate.verdict == "REJECT"
        assert any(str(patch_rel) in finding.path for finding in gate.findings)

        with pytest.raises(UsageError, match="approved review gate required"):
            update_work_item_status(conn, events, work_item_id=work_item_id, status="running")

        approved_rel = _write_gate_text(
            paths,
            "approved-gate.txt",
            GateResult(
                verdict="APPROVE",
                reason="static_checks_passed",
                findings=[],
            ),
        )
        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_item_id,
            kind="gate",
            path=str(approved_rel),
        )

        # Issue #87 — APPROVE alone is `approved_pending_apply`, still
        # blocking. The patch must also be recorded as applied.
        rejected = final_gate(paths)
        assert rejected.verdict == "REJECT"
        assert any("no `apply` artifact" in finding.message for finding in rejected.findings)

        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_item_id,
            kind="apply",
            path=str(patch_rel),
        )

        assert final_gate(paths).verdict == "APPROVE"
        updated = update_work_item_status(conn, events, work_item_id=work_item_id, status="running")
        assert updated["status"] == "running"
    finally:
        conn.close()


def test_review_gate_rejects_mismatched_diff_and_apply_patch(tmp_path: Path) -> None:
    """Issue #109 — a benign diff cannot be reviewed while a different,
    potentially dangerous patch is applied. If `--diff` and `--apply-patch`
    point at non-identical files, the gate must REJECT with
    `diff_apply_patch_mismatch` and the dangerous patch must not run."""
    import hashlib

    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _init_git_repo(paths.repo_root)
        work_item_id = _seed_work_item(conn, paths, events)
        approved_patch_rel, _target = _write_patch_artifact(conn, paths, events, work_item_id)

        # Reviewer would approve `approved.patch`. Attacker swaps in a
        # different patch that creates a brand-new file the reviewer
        # never saw.
        dangerous_patch = paths.patches_dir / work_item_id / "dangerous.patch"
        dangerous_patch.write_text(
            "diff --git a/owned.txt b/owned.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/owned.txt\n"
            "@@\n"
            "+pwned\n",
            encoding="utf-8",
        )
        rel_dangerous = Path(str(dangerous_patch.resolve().relative_to(paths.repo_root.resolve())))

        # Pre-flight: confirm the two files actually differ.
        diff_sha = hashlib.sha256((paths.repo_root / approved_patch_rel).read_bytes()).hexdigest()
        apply_sha = hashlib.sha256(dangerous_patch.read_bytes()).hexdigest()
        assert diff_sha != apply_sha

        reviewer_rel = _write_gate_text(
            paths,
            "reviewer-approve.txt",
            GateResult(verdict="APPROVE", reason="static_checks_passed", findings=[]),
        )
        (paths.repo_root / reviewer_rel).write_text(
            "verdict: APPROVE\n"
            "reason: static_checks_passed\n"
            "findings:\n"
            "- OK:1 - no blocking findings\n",
            encoding="utf-8",
        )

        result = run_review_gate(
            orch,
            paths,
            events,
            diff_path=approved_patch_rel,
            scope="api",
            reviewer_output_path=reviewer_rel,
            apply_patch_path=rel_dangerous,
            work_item_id=work_item_id,
        )

        assert result.ok is False
        manifest = json.loads((paths.repo_root / result.manifest_path).read_text(encoding="utf-8"))
        assert manifest["gate"]["reason"] == "diff_apply_patch_mismatch"
        # The dangerous patch must not have run.
        assert not (paths.repo_root / "owned.txt").exists()
        event_kinds = [event["kind"] for event in events.tail(100)]
        assert "gate.patch_blocked" in event_kinds
        assert "gate.patch_applied" not in event_kinds
    finally:
        conn.close()


def _seed_resolved_patch(conn, paths, events, work_item_id: str) -> Path:
    """Install patch + APPROVE gate + apply artifact so patch-resolution pillar passes."""
    patch_rel, _target = _write_patch_artifact(conn, paths, events, work_item_id)
    approved_rel = _write_gate_text(
        paths,
        "approved-gate.txt",
        GateResult(verdict="APPROVE", reason="static_checks_passed", findings=[]),
    )
    register_work_item_artifact(
        conn, paths, events, work_item_id=work_item_id, kind="gate", path=str(approved_rel)
    )
    register_work_item_artifact(
        conn, paths, events, work_item_id=work_item_id, kind="apply", path=str(patch_rel)
    )
    return patch_rel


def test_final_gate_rejects_when_no_run_report(tmp_path: Path) -> None:
    """Issue #90 — required files alone are not a readiness signal.
    Final gate must REJECT when `reports/last-run.json` is missing."""
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        _install_final_gate_files(paths)
        work_item_id = _seed_work_item(conn, paths, events)
        _seed_resolved_patch(conn, paths, events, work_item_id)
        (paths.repo_root / "reports" / "last-run.json").unlink()
        result = final_gate(paths)
        assert result.verdict == "REJECT"
        assert any(
            "[pillar=run_report]" in f.message and "missing reports/last-run.json" in f.message
            for f in result.findings
        )
    finally:
        conn.close()


def test_final_gate_rejects_product_failure_without_bug_record(tmp_path: Path) -> None:
    """Issue #90 — a failing scenario without `@known-bug`/`@bug-NNN` is a
    product red. The final gate must REJECT until a bug record exists."""
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        _install_final_gate_files(paths)
        work_item_id = _seed_work_item(conn, paths, events)
        _seed_resolved_patch(conn, paths, events, work_item_id)
        (paths.repo_root / "reports" / "last-run.json").write_text(
            json.dumps(
                {
                    "ran_at": "2026-01-01T00:00:00Z",
                    "total": 1,
                    "passed": 0,
                    "failed": 1,
                    "skipped": 0,
                    "failures": [
                        {
                            "scenario": "checkout rejects invalid card",
                            "tags": ["@functional-orders"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = final_gate(paths)
        assert result.verdict == "REJECT"
        assert any(
            "[pillar=bug_evidence]" in f.message
            and "checkout rejects invalid card" in f.message
            for f in result.findings
        )
    finally:
        conn.close()


def test_final_gate_rejects_known_bug_tag_without_bug_file(tmp_path: Path) -> None:
    """Issue #90 — a `@bug-NNN` tag must reference a real `bugs/BUG-NNN-*.md`."""
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        _install_final_gate_files(paths)
        work_item_id = _seed_work_item(conn, paths, events)
        _seed_resolved_patch(conn, paths, events, work_item_id)
        (paths.repo_root / "reports" / "last-run.json").write_text(
            json.dumps(
                {
                    "ran_at": "2026-01-01T00:00:00Z",
                    "total": 1,
                    "passed": 0,
                    "failed": 1,
                    "skipped": 0,
                    "failures": [
                        {
                            "scenario": "known bug remains red",
                            "tags": ["@known-bug", "@bug-042"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = final_gate(paths)
        assert result.verdict == "REJECT"
        assert any(
            "[pillar=known_bug_policy]" in f.message and "@bug-042" in f.message
            for f in result.findings
        )
    finally:
        conn.close()


def test_final_gate_approves_when_all_pillars_pass(tmp_path: Path) -> None:
    """Issue #90 — happy path: required files + resolved patch + green
    run report + matching bug file ⇒ APPROVE with all pillars `ok`."""
    from agentic_os.gates import evaluate_final_gate

    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        _install_final_gate_files(paths)
        work_item_id = _seed_work_item(conn, paths, events)
        _seed_resolved_patch(conn, paths, events, work_item_id)
        bugs_dir = paths.repo_root / "bugs"
        bugs_dir.mkdir(parents=True, exist_ok=True)
        (bugs_dir / "BUG-042-known-bug-remains-red.md").write_text(
            "# BUG-042\nremains red\n", encoding="utf-8"
        )
        (paths.repo_root / "reports" / "last-run.json").write_text(
            json.dumps(
                {
                    "ran_at": "2026-01-01T00:00:00Z",
                    "total": 2,
                    "passed": 1,
                    "failed": 1,
                    "skipped": 0,
                    "failures": [
                        {
                            "scenario": "known bug remains red",
                            "tags": ["@known-bug", "@bug-042"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        gate, pillars = evaluate_final_gate(paths)
        assert gate.verdict == "APPROVE", gate.findings
        # Non-blocking statuses: a pillar that does not apply (e.g. the
        # work_item_run_attestation pillar from #184 when no --work-item
        # is supplied) reports `skipped`. Only `failed` should block.
        assert all(
            p["status"] in {"ok", "skipped"} for p in pillars.values()
        ), pillars
        # Issue #90 pillars must always appear; extra pillars (such as
        # the work-item attestation pillar from #184) are allowed.
        assert {
            "required_files",
            "patch_resolution",
            "run_report",
            "bug_evidence",
            "known_bug_policy",
        }.issubset(set(pillars))
    finally:
        conn.close()


def test_review_gate_without_apply_leaves_patch_blocking_final_gate(tmp_path: Path) -> None:
    """Issue #87 — when the dashboard reviews a patch with
    `apply_patch_path=None` (review-only flow), the patch must remain
    `approved_pending_apply` so the final gate keeps blocking until a
    real apply step writes the `apply` artifact.
    """
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _init_git_repo(paths.repo_root)
        _install_final_gate_files(paths)
        work_item_id = _seed_work_item(conn, paths, events)
        patch_rel, _target = _write_patch_artifact(conn, paths, events, work_item_id)

        reviewer_rel = _write_gate_text(
            paths,
            "reviewer-approve.txt",
            GateResult(verdict="APPROVE", reason="static_checks_passed", findings=[]),
        )
        (paths.repo_root / reviewer_rel).write_text(
            "verdict: APPROVE\n"
            "reason: static_checks_passed\n"
            "findings:\n"
            "- OK:1 - no blocking findings\n"
            "READY\n",
            encoding="utf-8",
        )

        result = run_review_gate(
            orch,
            paths,
            events,
            diff_path=patch_rel,
            scope="api",
            reviewer_output_path=reviewer_rel,
            apply_patch_path=None,  # mimic dashboard review-only path
            work_item_id=work_item_id,
        )
        assert result.ok is True

        gate = final_gate(paths)
        assert gate.verdict == "REJECT"
        assert any("no `apply` artifact" in f.message for f in gate.findings)
        event_kinds = [event["kind"] for event in events.tail(200)]
        assert "gate.patch_applied" not in event_kinds
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Issue #287 — skill_failure producer: persistent reviewer REJECT clusters.
# ---------------------------------------------------------------------------


def _run_gate_with_verdict(
    orch: Orchestrator,
    paths: RuntimePaths,
    events: EventLog,
    work_item_id: str,
    *,
    scope: str,
    verdict: str,
    reason: str,
) -> None:
    """Drive run_review_gate once with a benign diff and a forced reviewer verdict.

    The benign skeleton patch passes the static gate so the reviewer output
    file decides the final verdict, letting the test simulate consecutive
    APPROVE / REJECT outcomes for one scope.
    """
    patch_rel, _target_rel = _write_patch_artifact(orch.conn, paths, events, work_item_id)
    reviewer_text = (
        f"verdict: {verdict}\n"
        f"reason: {reason}\n"
        "\n"
        "findings:\n"
        "- OK:1 - none\n"
        "READY\n"
    )
    reviewer_rel = paths.evidence_dir / f"reviewer-{verdict}-{ulid()}.txt"
    reviewer_rel.parent.mkdir(parents=True, exist_ok=True)
    reviewer_rel.write_text(reviewer_text, encoding="utf-8")
    rel = str(reviewer_rel.resolve().relative_to(paths.repo_root.resolve()))
    run_review_gate(
        orch,
        paths,
        events,
        diff_path=patch_rel,
        scope=scope,
        reviewer_output_path=rel,
        work_item_id=work_item_id,
    )


def _skill_failure_rows(conn: sqlite3.Connection, scope: str) -> list:
    return conn.execute(
        "SELECT subject, payload FROM learnings WHERE kind='skill_failure' AND subject=?;",
        (f"reviewer::{scope}",),
    ).fetchall()


def test_two_consecutive_rejects_record_skill_failure(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _init_git_repo(paths.repo_root)
        wid = _seed_work_item(conn, paths, events)

        # One REJECT alone is below the threshold → no learning.
        _run_gate_with_verdict(
            orch, paths, events, wid,
            scope="api", verdict="REJECT", reason="missing_negative_case",
        )
        assert _skill_failure_rows(conn, "api") == []

        # Second consecutive REJECT for the same scope reaches threshold (2)
        # → a skill_failure learning is recorded, clustering the reject reason.
        _run_gate_with_verdict(
            orch, paths, events, wid,
            scope="api", verdict="REJECT", reason="missing_negative_case",
        )
        rows = _skill_failure_rows(conn, "api")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload"])
        assert payload["reason"] == "missing_negative_case"
        assert payload["consecutive"] >= 2
        assert payload["scope"] == "api"
    finally:
        conn.close()


def test_approve_between_rejects_resets_streak(tmp_path: Path) -> None:
    conn, paths, events, orch = _runtime(tmp_path)
    try:
        _init_git_repo(paths.repo_root)
        wid = _seed_work_item(conn, paths, events)

        _run_gate_with_verdict(
            orch, paths, events, wid,
            scope="api", verdict="REJECT", reason="missing_negative_case",
        )
        # APPROVE breaks the streak.
        _run_gate_with_verdict(
            orch, paths, events, wid,
            scope="api", verdict="APPROVE", reason="looks_good",
        )
        # A single REJECT after the reset is again below threshold.
        _run_gate_with_verdict(
            orch, paths, events, wid,
            scope="api", verdict="REJECT", reason="missing_negative_case",
        )
        assert _skill_failure_rows(conn, "api") == []
    finally:
        conn.close()


def test_final_gate_requires_canonical_config_example_not_legacy(tmp_path: Path) -> None:
    """Issue #71 — `final_gate()` must require `config/agentic-os.yml.example`,
    not the removed `.qualitycat/agentic-os.yml.example`."""
    conn, paths, _events, _orch = _runtime(tmp_path)
    try:
        _install_final_gate_files(paths)
        result = final_gate(paths)
        assert result.verdict == "APPROVE", result.findings

        # Re-run after removing only the canonical example. Gate must
        # REJECT and the finding must name the canonical path, not the
        # legacy `.qualitycat/` path.
        (paths.repo_root / "config" / "agentic-os.yml.example").unlink()
        legacy = paths.repo_root / ".qualitycat" / "agentic-os.yml.example"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("legacy-fallback\n", encoding="utf-8")

        rejected = final_gate(paths)
        assert rejected.verdict == "REJECT"
        missing_paths = {f.path for f in rejected.findings}
        assert "config/agentic-os.yml.example" in missing_paths
        assert ".qualitycat/agentic-os.yml.example" not in missing_paths
    finally:
        conn.close()
