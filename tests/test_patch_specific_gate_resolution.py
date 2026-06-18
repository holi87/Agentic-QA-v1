"""Issue #104 — patch resolution must be patch-specific, not work-item-wide.

A gate artifact only resolves the exact patch it reviewed. Approving
patch A on a work item must not unblock a sibling patch B on the same
work item.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from agentic_os.events import EventLog
from agentic_os.gates import (
    GateResult,
    describe_blocking_patches,
    find_patch_gate_violations,
    write_gate_result,
)
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.work_items import (
    create_work_item_from_payload,
    register_work_item_artifact,
)


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
            "title": "Two-patch work item",
            "spec_path": "specs/two-patch.md",
            "priority": "P1",
            "sut_root": ".",
            "scenarios": ["s"],
        },
        default_sut_root=".",
    )
    return str(detail["work_item"]["id"])


def _write_patch(paths: RuntimePaths, work_item_id: str, name: str, body: str) -> tuple[Path, str]:
    target = paths.patches_dir / work_item_id / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    rel = str(target.resolve().relative_to(paths.repo_root.resolve()))
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return target, rel


def test_approving_one_patch_does_not_resolve_a_sibling_patch(tmp_path: Path) -> None:
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        work_item_id = _seed_work_item(conn, paths, events)
        # Patch A: registered first.
        patch_a, rel_a = _write_patch(
            paths,
            work_item_id,
            "a.patch",
            "diff --git a/x.txt b/x.txt\n--- /dev/null\n+++ b/x.txt\n@@\n+a\n",
        )
        register_work_item_artifact(
            conn, paths, events, work_item_id=work_item_id, kind="patch", path=rel_a
        )
        # Patch B: a different sibling registered later.
        patch_b, rel_b = _write_patch(
            paths,
            work_item_id,
            "b.patch",
            "diff --git a/y.txt b/y.txt\n--- /dev/null\n+++ b/y.txt\n@@\n+b\n",
        )
        register_work_item_artifact(
            conn, paths, events, work_item_id=work_item_id, kind="patch", path=rel_b
        )

        # Approve only Patch A — bind to rel_a explicitly.
        sha_a = hashlib.sha256(patch_a.read_bytes()).hexdigest()
        gate_a = write_gate_result(
            paths,
            GateResult(verdict="APPROVE", reason="static_checks_passed", findings=[]),
            name="review-gate",
            patch_metadata={"path": rel_a, "sha256": sha_a},
        )
        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_item_id,
            kind="gate",
            path=str(gate_a.relative_to(paths.repo_root)),
        )
        # Apply artifact for A.
        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_item_id,
            kind="apply",
            path=rel_a,
        )

        # Patch A is resolved; Patch B is still waiting.
        states = {
            row["patch_path"]: row
            for row in describe_blocking_patches(paths, conn=conn)
        }
        assert states[rel_a]["state"] == "approved"
        assert states[rel_a]["blocking"] is False
        assert states[rel_b]["state"] == "waiting", states[rel_b]
        assert states[rel_b]["blocking"] is True

        # Final-gate–style violations: patch B must still be flagged.
        violations = find_patch_gate_violations(paths, conn=conn)
        flagged = {v.path for v in violations}
        assert rel_b in flagged
        assert rel_a not in flagged
    finally:
        conn.close()


def test_apply_artifact_for_other_patch_does_not_resolve_this_one(tmp_path: Path) -> None:
    """Issue #104 + #87 — an `apply` artifact for patch A cannot satisfy
    patch B's "APPROVE + applied" requirement even when the gate that
    approved B has no binding (legacy)."""
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        work_item_id = _seed_work_item(conn, paths, events)
        _, rel_a = _write_patch(paths, work_item_id, "a.patch", "patch-a-body\n")
        _, rel_b = _write_patch(paths, work_item_id, "b.patch", "patch-b-body\n")
        for rel in (rel_a, rel_b):
            register_work_item_artifact(
                conn, paths, events, work_item_id=work_item_id, kind="patch", path=rel
            )

        # Bound APPROVE for B.
        gate_b = write_gate_result(
            paths,
            GateResult(verdict="APPROVE", reason="static_checks_passed", findings=[]),
            name="review-gate",
            patch_metadata={"path": rel_b, "sha256": "deadbeef"},
        )
        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_item_id,
            kind="gate",
            path=str(gate_b.relative_to(paths.repo_root)),
        )
        # An apply artifact, but for A, not B.
        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_item_id,
            kind="apply",
            path=rel_a,
        )

        states = {
            row["patch_path"]: row
            for row in describe_blocking_patches(paths, conn=conn)
        }
        # B is APPROVE-but-no-apply for its own path → still blocking.
        assert states[rel_b]["state"] == "approved_pending_apply"
        assert states[rel_b]["blocking"] is True
    finally:
        conn.close()


def test_legacy_unbound_gate_still_resolves_lone_patch(tmp_path: Path) -> None:
    """Backwards-compat — a pre-#104 gate artifact without a `patch:`
    binding still resolves a single patch on its work item."""
    conn, paths, events, _orch = _runtime(tmp_path)
    try:
        work_item_id = _seed_work_item(conn, paths, events)
        _, rel = _write_patch(paths, work_item_id, "p.patch", "legacy\n")
        register_work_item_artifact(
            conn, paths, events, work_item_id=work_item_id, kind="patch", path=rel
        )
        # Gate with no binding (legacy).
        legacy_gate = write_gate_result(
            paths,
            GateResult(verdict="APPROVE", reason="static_checks_passed", findings=[]),
            name="review-gate",
        )
        register_work_item_artifact(
            conn,
            paths,
            events,
            work_item_id=work_item_id,
            kind="gate",
            path=str(legacy_gate.relative_to(paths.repo_root)),
        )
        register_work_item_artifact(
            conn, paths, events, work_item_id=work_item_id, kind="apply", path=rel
        )
        states = describe_blocking_patches(paths, conn=conn)
        assert states[0]["state"] == "approved"
        assert states[0]["blocking"] is False
    finally:
        conn.close()
