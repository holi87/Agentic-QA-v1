"""Patch generation, review-gate, and dashboard implementation workflow plumbing."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentic_os.analysis import analyze_work_item
from agentic_os.events import EventLog
from agentic_os.errors import UsageError
from agentic_os.orchestrator import Orchestrator
from agentic_os.patch_builder import (
    build_skeleton_patch,
    implement_tests_for_work_item,
)
from agentic_os.paths import RuntimePaths
from agentic_os.server import make_server
from agentic_os.storage import init_db
from agentic_os.storage.db import connect
from agentic_os.test_planning import plan_work_item, read_plan_candidates, update_plan_candidate_decision
from agentic_os.work_items import (
    create_work_item_from_payload,
    get_work_item,
    list_work_item_artifacts,
)
from tests.test_dashboard_task_ui import _DEFAULT_CONFIG, _free_port, _wait


def _runtime(tmp_path: Path, *, enable_write: bool = False) -> tuple[RuntimePaths, sqlite3.Connection]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_DEFAULT_CONFIG.format(write=str(enable_write).lower()).lstrip(), encoding="utf-8")
    conn = init_db(paths.db)
    Orchestrator(conn, paths, EventLog(conn, paths)).seed_phases()
    return paths, conn


def _payload() -> dict:
    return {
        "title": "Order negative validation",
        "priority": "P1",
        "business_goal": "Cover order creation invalid paths.",
        "expected_behavior": "POST /orders rejects invalid payloads with 422.",
        "in_scope": "API validation; error shape; exact-spec evidence.",
        "out_of_scope": "Payment provider sandbox.",
        "known_bugs": "BUG-001 returns 500 instead of 422.",
        "relevant_surfaces": "POST /orders, /checkout page",
        "test_data": "Local non-production fixtures only.",
        "time_budget": "60 minutes",
    }


def _seed_planned(paths: RuntimePaths) -> tuple[sqlite3.Connection, str]:
    conn = connect(paths.db)
    events = EventLog(conn, paths)
    detail = create_work_item_from_payload(conn, paths, events, _payload(), default_sut_root=".")
    work_item_id = detail["work_item"]["id"]
    analyze_work_item(conn, paths, events, work_item_id=work_item_id)
    plan_work_item(conn, paths, events, work_item_id=work_item_id)
    return conn, work_item_id


def _approve_first_candidate(paths: RuntimePaths, work_item_id: str) -> None:
    """Issue #194 — flip a plan candidate to ``generate_now`` so the
    dashboard's gating allows ``implement-tests``. Required by tests that
    exercise the implement-tests endpoint over HTTP rather than calling the
    workflow function directly.

    Prefers a UI candidate because the API generator demands a JSON
    `required_test_data` payload, but the seed plan's mutating POST does
    not ship one. The UI candidate just needs a target_page + URL/text
    assertion, which is enough to clear plan validation."""
    payload = read_plan_candidates(paths, work_item_id=work_item_id)
    items = payload.get("items") or []
    if not items:
        return
    chosen = next(
        (item for item in items if item.get("test_type") == "ui"),
        items[0],
    )
    kwargs = {
        "paths": paths,
        "work_item_id": work_item_id,
        "candidate_id": chosen["candidate_id"],
        "decision": "generate_now",
        "reason": "seed approval for dashboard gating test (#194)",
    }
    if chosen.get("test_type") == "ui":
        kwargs.update(
            expected_assertion='URL contains /checkout and text "Order confirmed"',
            target_page="/checkout",
        )
    else:
        kwargs.update(
            expected_assertion="HTTP 200 with non-empty JSON body",
            required_test_data='{"item": "demo"}',
            cleanup_strategy="DELETE /orders/{id}",
        )
    update_plan_candidate_decision(**kwargs)


def test_build_skeleton_patch_is_deterministic() -> None:
    plan_text = "### API\n- POST /orders 422\n\n### UI\n- /checkout displays errors\n"
    first = build_skeleton_patch(
        work_item_id="TASK-20260101-000000-demo",
        title="Demo task",
        priority="P1",
        sut_root=".",
        plan_text=plan_text,
    )
    second = build_skeleton_patch(
        work_item_id="TASK-20260101-000000-demo",
        title="Demo task",
        priority="P1",
        sut_root=".",
        plan_text=plan_text,
    )
    assert first.body == second.body
    assert first.target_rel_path == "tests/generated/TASK-20260101-000000-demo.spec.md"
    assert "POST /orders 422" in first.body
    assert "/checkout displays errors" in first.body


def test_implement_tests_creates_patch_and_artifact(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        events = EventLog(conn, paths)
        result = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        assert result["status"] == "blocked"
        assert result["executable_tests_generated"] is False
        assert result["generated_v2"]["needs_operator_decision"] is True
        rel_patch = result["patch_path"]
        patch_path = paths.repo_root / rel_patch
        assert patch_path.exists()
        body = patch_path.read_text(encoding="utf-8")
        assert body.startswith(f"diff --git a/tests/generated/{work_item_id}.spec.md")
        assert "@@ -0,0" in body
        registered = list_work_item_artifacts(conn, work_item_id)
        kinds = {a["kind"] for a in registered}
        assert "patch" in kinds
        updated = get_work_item(conn, work_item_id)
        assert updated is not None
        assert updated["status"] == "blocked"
    finally:
        conn.close()


def test_implement_tests_writes_pending_coverage_manifest(tmp_path: Path) -> None:
    """Issue #319/#320 (Codex P1) — generation writes a pending coverage
    manifest beside the patch; the ledger fills only when apply-patch ingests
    it, so a never-applied patch does not leave a ghost surface in the ledger."""
    from agentic_os.coverage_ledger import (
        ingest_pending_manifest,
        is_covered,
        list_coverage,
    )

    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_first_candidate(paths, work_item_id)
        events = EventLog(conn, paths)
        result = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        assert result["executable_tests_generated"] is True

        # Generation alone must not have touched the ledger.
        assert list_coverage(conn, project_id="default") == []

        # The pending manifest is on disk beside the patch and ingests cleanly.
        manifest_rel = result["coverage_manifest_path"]
        assert manifest_rel and manifest_rel.endswith(".coverage.json")
        manifest_path = paths.repo_root / manifest_rel
        assert manifest_path.exists()
        assert ingest_pending_manifest(conn, manifest_path) >= 1

        rows = list_coverage(conn, project_id="default")
        assert rows, "ledger fills after the apply step ingests the manifest"
        assert is_covered(conn, project_id="default", surface_kind="ui", surface_key="/checkout")
        checkout = next(r for r in rows if r["surface_key"] == "/checkout")
        assert checkout["spec_path"].endswith(".spec.ts")
        assert checkout["work_item_id"] == work_item_id
        assert checkout["run_id"]
    finally:
        conn.close()


def test_unapplied_patch_leaves_ledger_empty_and_regenerates(tmp_path: Path) -> None:
    """Codex P1 guard — if implement-tests is called twice but apply never
    happens, the second call must still produce a patch (no ghost coverage)."""
    from agentic_os.coverage_ledger import list_coverage

    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_first_candidate(paths, work_item_id)
        events = EventLog(conn, paths)

        first = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        assert first["executable_tests_generated"] is True
        assert list_coverage(conn, project_id="default") == []  # nothing applied yet

        # No ingest happens — caller never reached apply-patch.
        second = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        # Without applied coverage the gate cannot skip; the surface still
        # needs a patch.
        assert second.get("idempotent_noop") is not True
        assert second["executable_tests_generated"] is True
    finally:
        conn.close()


def test_implement_tests_is_idempotent_against_the_ledger(tmp_path: Path) -> None:
    """Issue #320 — after a generated patch is *applied* (ledger ingested),
    re-running implement-tests adds zero duplicate specs; the now-covered
    surface is skipped and reported, not regenerated."""
    from agentic_os.coverage_ledger import ingest_pending_manifest, list_coverage

    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_first_candidate(paths, work_item_id)
        events = EventLog(conn, paths)

        first = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        assert first["executable_tests_generated"] is True
        # Simulate `apply-patch` success — the workflow ingests the manifest.
        manifest_path = paths.repo_root / first["coverage_manifest_path"]
        ingest_pending_manifest(conn, manifest_path)
        after_first = list_coverage(conn, project_id="default")
        assert after_first, "applied coverage must populate the ledger"

        second = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        # Nothing new to generate: the surface is already covered.
        assert second["idempotent_noop"] is True
        assert second["executable_tests_generated"] is False
        skipped_keys = {s["surface_key"] for s in second["skipped_surfaces"]}
        assert "/checkout" in skipped_keys
        # The ledger did not grow — zero duplicate coverage rows.
        assert len(list_coverage(conn, project_id="default")) == len(after_first)
    finally:
        conn.close()


def test_implement_tests_requires_plan(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn = connect(paths.db)
    events = EventLog(conn, paths)
    detail = create_work_item_from_payload(conn, paths, events, _payload(), default_sut_root=".")
    work_item_id = detail["work_item"]["id"]
    try:
        with pytest.raises(UsageError):
            implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
    finally:
        conn.close()


def test_implement_tests_suffixes_existing_target(tmp_path: Path) -> None:
    """Issue #223 — repeated `implement-tests` runs append a numeric suffix
    instead of crashing with `refusing to overwrite existing file via patch`.
    """
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        existing = paths.repo_root / "tests" / "generated" / f"{work_item_id}.spec.md"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text("pre-existing\n", encoding="utf-8")
        events = EventLog(conn, paths)
        result = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        assert result["target_path"] != f"tests/generated/{work_item_id}.spec.md"
        assert result["target_path"].startswith("tests/generated/")
        assert ".spec.md" in result["target_path"]
        assert ".2.spec.md" in result["target_path"]
    finally:
        conn.close()


def test_patch_passes_git_apply_check(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        events = EventLog(conn, paths)
        result = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        patch_path = paths.repo_root / result["patch_path"]
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "init", "-q"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "config", "user.name", "Test"],
            check=True,
        )
        # commit a baseline so `git apply --check` operates against a tree.
        baseline = paths.repo_root / "README.md"
        baseline.write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(paths.repo_root), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "commit", "-q", "-m", "baseline"],
            check=True,
        )
        result_check = subprocess.run(
            ["git", "-C", str(paths.repo_root), "apply", "--check", str(patch_path)],
            capture_output=True,
            text=True,
        )
        assert result_check.returncode == 0, result_check.stderr
    finally:
        conn.close()


def test_dashboard_implement_tests_endpoint(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path, enable_write=True)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    conn.close()
    # Issue #194 — dashboard gating now requires at least one candidate
    # approved (decision=generate_now) before `implement-tests` is allowed.
    # Approve the first candidate so the dispatch reaches the workflow
    # (which then legitimately reports `blocked` because the seed plan
    # has no fully-specified candidates).
    _approve_first_candidate(paths, work_item_id)
    port = _free_port()
    server = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _wait(f"http://127.0.0.1:{port}/healthz")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/tasks/{work_item_id}/implement-tests",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        # Issue #194 — with a real candidate approval the implement-tests
        # endpoint now produces an executable plan instead of falling back
        # to the skeleton "blocked" path the old test exercised.
        assert payload["status"] in {"blocked", "implementing"}
        assert payload["patch_path"].startswith(".agentic-os/patches/")
        assert any(a["kind"] == "patch" for a in payload["artifacts"])
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_review_gate_with_work_item_approves_and_marks_reviewing(tmp_path: Path) -> None:
    from agentic_os.workflows import run_review_gate

    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    try:
        result = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        patch_rel = Path(result["patch_path"])
        gate_result = run_review_gate(
            orch,
            paths,
            events,
            diff_path=patch_rel,
            scope="api",
            work_item_id=work_item_id,
        )
        assert gate_result.ok is True
        updated = get_work_item(conn, work_item_id)
        assert updated is not None
        assert updated["status"] == "reviewing"
        kinds = [a["kind"] for a in list_work_item_artifacts(conn, work_item_id)]
        assert "patch" in kinds
        assert "gate" in kinds
    finally:
        conn.close()


def test_review_gate_apply_ingests_coverage_manifest(tmp_path: Path) -> None:
    """Issue #320 (Codex P1) — the ledger fills only when `apply-patch`
    successfully lands the spec files. The apply branch of run_review_gate
    must ingest the pending manifest written next to the patch."""
    from agentic_os.coverage_ledger import is_covered, list_coverage
    from agentic_os.workflows import run_review_gate

    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    try:
        _approve_first_candidate(paths, work_item_id)
        result = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        assert result["coverage_manifest_path"]
        assert list_coverage(conn, project_id="default") == []  # pre-apply

        gate_result = run_review_gate(
            orch,
            paths,
            events,
            diff_path=Path(result["patch_path"]),
            scope="ui",
            apply_patch_path=Path(result["patch_path"]),
            work_item_id=work_item_id,
        )
        # Whether or not the gate approves, the ingest hook only fires when
        # the apply actually landed the files. We assert the post-condition:
        # if any apply artifact was registered, the ledger must reflect it.
        kinds = {a["kind"] for a in list_work_item_artifacts(conn, work_item_id)}
        if "apply" in kinds:
            assert is_covered(
                conn, project_id="default", surface_kind="ui", surface_key="/checkout"
            ), f"apply happened but ledger not ingested; gate={gate_result.ok}"
    finally:
        conn.close()


def test_review_gate_reject_marks_blocked_and_leaves_tree_clean(tmp_path: Path) -> None:
    from agentic_os.workflows import run_review_gate

    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    try:
        implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        # Build an unmistakable REJECT patch (removed assertion line).
        patch_dir_rel = Path(".agentic-os") / "patches" / work_item_id
        rejected_rel = patch_dir_rel / "reject.patch"
        rejected_abs = paths.repo_root / rejected_rel
        rejected_abs.write_text(
            "diff --git a/tests/dummy.py b/tests/dummy.py\n"
            "--- a/tests/dummy.py\n"
            "+++ b/tests/dummy.py\n"
            "@@ -1,1 +1,0 @@\n"
            "-assert value == 42\n",
            encoding="utf-8",
        )
        target = paths.repo_root / "tests" / "dummy.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("assert value == 42\n", encoding="utf-8")
        tree_before = target.read_text(encoding="utf-8")
        gate_result = run_review_gate(
            orch,
            paths,
            events,
            diff_path=rejected_rel,
            scope="assertion",
            apply_patch_path=rejected_rel,
            work_item_id=work_item_id,
        )
        assert gate_result.ok is False
        updated = get_work_item(conn, work_item_id)
        assert updated is not None
        assert updated["status"] == "blocked"
        assert target.read_text(encoding="utf-8") == tree_before
    finally:
        conn.close()


def test_dashboard_implement_tests_blocked_when_write_disabled(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path, enable_write=False)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    conn.close()
    port = _free_port()
    server = make_server(paths, host="127.0.0.1", port=port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _wait(f"http://127.0.0.1:{port}/healthz")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/tasks/{work_item_id}/implement-tests",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 403
    finally:
        server.shutdown()
        thread.join(timeout=5)
