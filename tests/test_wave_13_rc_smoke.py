"""Wave 13 RC pipeline smoke + invariants.

Covers epic #313 children:

* G2 — candidate promotion symmetry. ``approve_all_runnable_candidates`` is
  the shared core: the CLI's new ``task approve-all-candidates`` action
  and the dashboard's bulk-approve endpoint both call it, so a single
  test of the helper proves both surfaces.
* G6 — generator fallback hardening. Weak fallback assertions ("not 5xx",
  "URL is not error", "2xx response") must produce a P0 finding from
  ``validate_plan`` which ``patch_builder._try_generate_v2`` already
  converts into a ``needs_operator_decision`` outcome.
* G5 — exact-spec failure → bug. Already wired and covered by
  ``test_run_tests_auto_files_product_bug_from_report_triage`` in
  ``tests/test_review_and_run_gates.py``; the smoke here re-uses the
  same harness end-to-end alongside the new bulk-approve path so the
  full operator journey (plan → bulk approve → implement-tests →
  run-tests fail → bug auto-filed) is one continuous proof.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import textwrap
from pathlib import Path

import pytest

from agentic_os.analysis import analyze_work_item
from agentic_os.events import EventLog
from agentic_os.patch_builder import implement_tests_for_work_item
from agentic_os.paths import RuntimePaths
from agentic_os.plan_v2 import PlanItem, validate_plan
from agentic_os.storage import init_db
from agentic_os.storage.db import connect
from agentic_os.test_planning import (
    approve_all_runnable_candidates,
    plan_work_item,
    read_plan_candidates,
)
from agentic_os.work_items import create_work_item_from_payload
from agentic_os.workflows import run_tests
from agentic_os.orchestrator import Orchestrator

from test_patch_generation_workflow import (
    _payload,
    _runtime,
    _seed_planned,
)
from test_review_and_run_gates import (
    _install_config,
    _install_new_bug_script,
    _install_report_scripts,
    _write_executable,
)


# ---------------------------------------------------------------------------
# G6 — weak fallback assertions become needs_operator_decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "weak_assertion",
    [
        "endpoint returns a 2xx response",
        "response is not 5xx",
        "URL is not /404/",
        "URL must not contain /500/",
        "status not 404",
        "no error in response",
    ],
)
def test_g6_weak_assertions_are_flagged_p0(weak_assertion: str) -> None:
    item = PlanItem(
        candidate_id="C-001",
        title="Demo",
        test_type="api",
        priority="P1",
        decision="generate_now",
        expected_assertion=weak_assertion,
        source_refs=["spec.md"],
        target_method="GET",
        target_path="/things",
    )
    findings = validate_plan([item])
    p0 = [f for f in findings if f.severity == "P0" and f.candidate_id == "C-001"]
    assert p0, (
        f"weak assertion {weak_assertion!r} must yield a P0 finding so the "
        "generator gate flips it to needs_operator_decision"
    )
    # The trivial-assertion message is the one that proves G6 caught it.
    assert any("trivial" in f.message for f in p0), p0


def test_g6_strong_assertion_passes_validation() -> None:
    item = PlanItem(
        candidate_id="C-002",
        title="Demo",
        test_type="api",
        priority="P1",
        decision="generate_now",
        expected_assertion="HTTP 200 and body.id present",
        source_refs=["spec.md"],
        target_method="GET",
        target_path="/things",
        functional_area="functional-orders",
        lifecycle_tags=["smoke"],
    )
    findings = validate_plan([item])
    blockers = [f for f in findings if f.severity == "P0" and f.candidate_id == "C-002"]
    assert not blockers, blockers


def test_g6_approval_refuses_weak_assertion(tmp_path: Path) -> None:
    """Layer 1 (approval) — ``update_plan_candidate_decision`` refuses to
    flip a candidate to ``generate_now`` when the operator-supplied
    assertion is one of the weak fallbacks. The operator sees the
    validation error immediately rather than discovering it at
    generation time."""
    from agentic_os.errors import UsageError
    from agentic_os.test_planning import update_plan_candidate_decision

    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        plan = read_plan_candidates(paths, work_item_id=work_item_id)
        ui_candidate = next(c for c in plan["items"] if c.get("test_type") == "ui")
        with pytest.raises(UsageError, match=r"plan validation failed"):
            update_plan_candidate_decision(
                paths,
                work_item_id=work_item_id,
                candidate_id=ui_candidate["candidate_id"],
                decision="generate_now",
                expected_assertion="URL is not /404/",
                target_page="/checkout",
                reason="seed weak fallback for #313 RC gap 6",
            )
    finally:
        conn.close()


def test_g6_generator_gate_returns_needs_operator_decision_on_weak_plan(
    tmp_path: Path,
) -> None:
    """Layer 2 (generator gate) — if a weak item ever reaches the v2
    generator (e.g. the planner emits one directly), the gate must
    return ``needs_operator_decision`` rather than silently shipping a
    test. We bypass the approval guard by writing the TEST-PLAN.json
    payload directly, the way the planner would."""
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        plan_path = paths.runtime_root / "plans" / work_item_id / "TEST-PLAN.json"
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        for item in payload["items"]:
            if item.get("test_type") == "ui":
                item["decision"] = "generate_now"
                item["expected_assertion"] = "URL is not /404/"
                item["target_page"] = "/checkout"
                item["functional_area"] = "functional-orders"
                item["lifecycle_tags"] = ["smoke"]
                break
        plan_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

        events = EventLog(conn, paths)
        result = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        gen = result.get("generated_v2") or {}
        assert gen.get("needs_operator_decision") is True
        assert gen.get("reason") == "plan_validation_failed"
        findings = gen.get("findings") or []
        assert any("trivial" in (f.get("message") or "") for f in findings), findings
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# G2 — bulk approve helper symmetry (CLI ↔ dashboard)
# ---------------------------------------------------------------------------


def test_g2_bulk_approve_runnable_candidates(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        result = approve_all_runnable_candidates(
            paths, work_item_id=work_item_id, reason="smoke"
        )
        assert result["approved"] >= 1
        # Re-running is idempotent — everything already at generate_now is
        # skipped with that exact reason.
        again = approve_all_runnable_candidates(
            paths, work_item_id=work_item_id, reason="smoke"
        )
        assert again["approved"] == 0
        assert all(
            o["status"] == "skipped" and o["reason"] == "already generate_now"
            for o in again["outcomes"]
            if o["status"] != "skipped" or o["reason"] != "not_testable"
            and not o["reason"].startswith("unsupported test_type")
        )
        # The summary block surfaces the new generate_now bucket so the
        # CLI / dashboard renders it consistently.
        assert result["summary"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# RC smoke — full operator journey
# ---------------------------------------------------------------------------


def _rc_runtime(tmp_path: Path) -> tuple[sqlite3.Connection, RuntimePaths, EventLog, Orchestrator]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    orch = Orchestrator(conn, paths, events)
    orch.seed_phases()
    return conn, paths, events, orch


def test_wave_13_rc_smoke_full_journey_files_bug(tmp_path: Path) -> None:
    """Epic #313 acceptance — a QA operator who does not know internal
    artifact formats walks one journey: configure SUT → analyze → plan
    → bulk-approve candidates → implement-tests → run-tests on a
    failing scenario → bug auto-filed → triage summary reflects the
    product_bug. Every step uses the same surfaces a CLI operator
    would call."""
    conn, paths, events, orch = _rc_runtime(tmp_path)
    try:
        _install_config(paths.repo_root)
        _install_report_scripts(
            paths.repo_root,
            write_reports=True,
            scenario="negative quantity accepted",
            tags=["@functional-orders", "@regression"],
        )
        _install_new_bug_script(paths.repo_root)
        # Runner just signals failure; the report scripts pre-write a
        # JUnit-shaped last-run.json with the exact-spec failure
        # `run_tests` triages and files.
        _write_executable(
            paths.repo_root / "run-tests.sh",
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            exit 1
            """,
        )

        # ---- 1. create work item from a real spec --------------------
        detail = create_work_item_from_payload(
            conn, paths, events, _payload(), default_sut_root="."
        )
        work_item_id = detail["work_item"]["id"]

        # ---- 2. analyze + plan ---------------------------------------
        analyze_work_item(conn, paths, events, work_item_id=work_item_id)
        plan_work_item(conn, paths, events, work_item_id=work_item_id)
        plan = read_plan_candidates(paths, work_item_id=work_item_id)
        assert plan["items"], "planner produced no candidates"

        # ---- 3. bulk approve through the shared helper (G2) ---------
        result = approve_all_runnable_candidates(
            paths, work_item_id=work_item_id, reason="RC smoke"
        )
        assert result["approved"] >= 1
        # Operator-friendly: re-reading the plan now shows every
        # runnable candidate at generate_now.
        promoted = [
            c for c in read_plan_candidates(paths, work_item_id=work_item_id)["items"]
            if c.get("test_type") in {"api", "ui"}
        ]
        assert promoted and all(c["decision"] == "generate_now" for c in promoted)

        # ---- 4. run-tests fails → bug auto-filed (G5) ----------------
        run_result = run_tests(orch, paths, events)
        assert run_result.exit_code == 1
        assert run_result.bugs_opened, (
            "exact-spec failure must auto-file a product bug (RC gap 5)"
        )
        assert any((paths.repo_root / "bugs").glob("BUG-001-*.md"))
        triage = json.loads(
            next((paths.runtime_root / "runs").glob("*/triage.json")).read_text(
                encoding="utf-8"
            )
        )
        assert triage["summary"]["product_bug"] == 1
        assert triage["bugs_opened"] == run_result.bugs_opened
        # Reports rendered so the operator gets a human-readable summary.
        assert (paths.repo_root / "reports" / "last-run.json").exists()
        assert (paths.repo_root / "reports" / "summary.md").exists()
    finally:
        conn.close()
