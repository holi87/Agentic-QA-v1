"""#360 — implementer fan-out: deterministic, byte-stable, family-disjoint merge.

The implementer generates test families (API vs UI). #360 fans that generation
out into independent units (one per family / feature file), each owning a
**disjoint** output file set, then merges them into a single patch under one
serialized review gate (#361). The merge contract is the real correctness
content: the merged patch must be **byte-stable across runs** and carry **no
cross-family file collisions**, regardless of unit execution order.

These are characterization tests pinning that contract on the current serial
path — they MUST stay green after the fan-out refactor (the proof that the
parallel and serial paths are equivalent). The generators are pure CPU today,
so threading buys no wall-clock; the deliverable is a parallel-*ready* merge,
not a claimed speedup.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from agentic_os.analysis import analyze_work_item
from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.patch_builder import (
    _detect_path_collisions,
    _implement_idempotency_key,
    implement_tests_for_work_item,
)
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.storage.db import connect
from agentic_os.test_planning import (
    plan_work_item,
    read_plan_candidates,
    update_plan_candidate_decision,
)
from agentic_os.work_items import create_work_item_from_payload
from tests.test_dashboard_task_ui import _DEFAULT_CONFIG

# Multi-surface payload → the planner emits 3 API + 3 UI candidates, so the
# fixture exercises within-family ordering AND cross-family ordering.
_PAYLOAD = {
    "title": "Order lifecycle coverage",
    "priority": "P1",
    "business_goal": "Cover order create/read/delete + key pages.",
    "expected_behavior": (
        "POST /orders rejects invalid payloads with 422; "
        "GET /orders/{id} returns the order; "
        "DELETE /orders/{id} removes it."
    ),
    "in_scope": "API validation; error shape; page smoke.",
    "out_of_scope": "Payment provider sandbox.",
    "known_bugs": "none",
    "relevant_surfaces": (
        "POST /orders, GET /orders/{id}, DELETE /orders/{id}, "
        "/checkout page, /orders page"
    ),
    "test_data": "Local non-production fixtures only.",
    "time_budget": "60 minutes",
}


def _runtime(tmp_path: Path) -> RuntimePaths:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    cfg = repo / ".qualitycat" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(_DEFAULT_CONFIG.format(write="false").lstrip(), encoding="utf-8")
    conn = init_db(paths.db)
    Orchestrator(conn, paths, EventLog(conn, paths)).seed_phases()
    conn.close()
    return paths


def _seed_planned(paths: RuntimePaths) -> tuple[sqlite3.Connection, str]:
    conn = connect(paths.db)
    events = EventLog(conn, paths)
    detail = create_work_item_from_payload(conn, paths, events, _PAYLOAD, default_sut_root=".")
    work_item_id = detail["work_item"]["id"]
    analyze_work_item(conn, paths, events, work_item_id=work_item_id)
    plan_work_item(conn, paths, events, work_item_id=work_item_id)
    return conn, work_item_id


def _approve_two_api_one_ui(paths: RuntimePaths, work_item_id: str) -> None:
    """Approve ≥2 API + ≥1 UI candidates with fully-specified metadata so the
    generator emits multiple files across both families."""
    items = read_plan_candidates(paths, work_item_id=work_item_id).get("items") or []
    api = [c for c in items if c.get("test_type") == "api"][:2]
    ui = [c for c in items if c.get("test_type") == "ui"][:1]
    for cand in api:
        update_plan_candidate_decision(
            paths=paths,
            work_item_id=work_item_id,
            candidate_id=cand["candidate_id"],
            decision="generate_now",
            reason="#360 fan-out fixture",
            expected_assertion="HTTP 200 and body.id present",
            required_test_data='{"item": "demo"}',
            cleanup_strategy="DELETE /orders/{id}",
        )
    for cand in ui:
        update_plan_candidate_decision(
            paths=paths,
            work_item_id=work_item_id,
            candidate_id=cand["candidate_id"],
            decision="generate_now",
            reason="#360 fan-out fixture",
            expected_assertion='URL contains /orders and text "Orders"',
            target_page=cand.get("target_page") or "/orders",
        )


def _generate_patch_body(tmp_path: Path) -> tuple[str, str]:
    """Seed → approve → generate; return (executable patch diff body, work_item_id)."""
    paths = _runtime(tmp_path)
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_two_api_one_ui(paths, work_item_id)
        events = EventLog(conn, paths)
        result = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        assert result["executable_tests_generated"] is True, result
        body = (paths.repo_root / result["patch_path"]).read_text(encoding="utf-8")
        return body, work_item_id
    finally:
        conn.close()


def _spec_paths_in_order(diff_body: str) -> list[str]:
    """Target spec paths in the order they appear in the unified diff."""
    return re.findall(r"^\+\+\+ b/(\S+)", diff_body, flags=re.MULTILINE)


def test_multifamily_patch_is_byte_stable_across_runs(tmp_path: Path) -> None:
    """Two independent runs of the identical task produce a byte-identical
    executable patch once per-task identity (the work_item_id, the only
    legitimate per-run variant) is normalized. If the merge leaked a run_id /
    ULID / set-ordering, the bodies would still differ after this normalization
    — so equality here proves the merge carries no other nondeterminism."""
    first, wid_a = _generate_patch_body(tmp_path / "run_a")
    second, wid_b = _generate_patch_body(tmp_path / "run_b")
    assert first.replace(wid_a, "WID") == second.replace(wid_b, "WID")


def test_multifamily_patch_covers_both_families_disjointly(tmp_path: Path) -> None:
    """The merged patch carries API + UI spec files, each path unique (no
    cross-family collision), with API specs composed before UI specs."""
    body, _ = _generate_patch_body(tmp_path)
    paths = _spec_paths_in_order(body)
    assert paths, "patch carried no spec files"
    # No file collision across the merged units.
    assert len(paths) == len(set(paths)), f"duplicate target paths: {paths}"
    api_paths = [p for p in paths if "/api/" in p]
    ui_paths = [p for p in paths if "/ui/" in p]
    assert api_paths, f"no API specs in patch: {paths}"
    assert ui_paths, f"no UI specs in patch: {paths}"
    # Fixed cross-family order: every API spec precedes every UI spec.
    last_api = max(paths.index(p) for p in api_paths)
    first_ui = min(paths.index(p) for p in ui_paths)
    assert last_api < first_ui, f"families not in fixed api→ui order: {paths}"


def test_fanout_fails_loud_when_a_family_generator_errors(tmp_path: Path) -> None:
    """A unit whose generator raises must fail the implement step LOUDLY —
    `fan_out` masks the error, so #360's join barrier must re-raise it rather
    than silently shipping a patch missing that family. (Graceful partial-
    failure handling is #362's scope, not #360's.)"""
    import pytest

    from agentic_os.errors import UsageError

    paths = _runtime(tmp_path)
    conn, work_item_id = _seed_planned(paths)
    try:
        # Approve an API candidate whose assertion passes plan validation but
        # the generator cannot convert into an executable assertion (mentions
        # "body" with no parseable shape) → generate_api_tests raises UsageError.
        items = read_plan_candidates(paths, work_item_id=work_item_id).get("items") or []
        api = next(c for c in items if c.get("test_type") == "api")
        update_plan_candidate_decision(
            paths=paths,
            work_item_id=work_item_id,
            candidate_id=api["candidate_id"],
            decision="generate_now",
            reason="#360 fail-loud fixture",
            expected_assertion="HTTP 200 with a JSON body",
            required_test_data='{"item": "demo"}',
            cleanup_strategy="DELETE /orders/{id}",
        )
        events = EventLog(conn, paths)
        with pytest.raises(UsageError):
            implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
    finally:
        conn.close()


# ---- #362: per-work-unit idempotency keys --------------------------------

def _spec(candidate_id: str, relative_path: str) -> SimpleNamespace:
    """Minimal stand-in for a generated spec (only the fields the key reads)."""
    return SimpleNamespace(candidate_id=candidate_id, relative_path=relative_path)


def test_idempotency_key_is_deterministic_and_order_independent() -> None:
    """Same work item + family + surface set → identical key, regardless of the
    order the fan-out merge happened to emit specs in. This is what lets a
    rerun be recognized as the same work-unit instead of duplicated."""
    specs = [_spec("c1", "tests/api/a.spec.ts"), _spec("c2", "tests/api/b.spec.ts")]
    reordered = list(reversed(specs))
    k1 = _implement_idempotency_key("wi-1", "api", specs)
    k2 = _implement_idempotency_key("wi-1", "api", specs)
    k3 = _implement_idempotency_key("wi-1", "api", reordered)
    assert k1 == k2 == k3
    assert k1.startswith("implement-tests:api:")
    assert re.fullmatch(r"implement-tests:api:[0-9a-f]{64}", k1), k1


def test_idempotency_key_is_input_family_and_workitem_sensitive() -> None:
    """The key changes when the unit's identity changes — different work item,
    different family, or a different approved surface set all yield a new key,
    so a rerun whose inputs changed is NOT mistaken for the prior unit."""
    base = [_spec("c1", "tests/api/a.spec.ts")]
    key = _implement_idempotency_key("wi-1", "api", base)
    assert key != _implement_idempotency_key("wi-2", "api", base)  # work item
    assert key != _implement_idempotency_key("wi-1", "ui", base)   # family
    assert key != _implement_idempotency_key(  # different surface
        "wi-1", "api", [_spec("c1", "tests/api/other.spec.ts")]
    )
    assert key != _implement_idempotency_key(  # added surface
        "wi-1", "api", base + [_spec("c2", "tests/api/b.spec.ts")]
    )


def test_implement_surfaces_one_idempotency_key_per_family(tmp_path: Path) -> None:
    """A real generation run surfaces one well-formed key per fan-out family on
    the result (under generated_v2) — the observable handle #362 adds."""
    paths = _runtime(tmp_path)
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_two_api_one_ui(paths, work_item_id)
        events = EventLog(conn, paths)
        result = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        keys = result["generated_v2"]["idempotency_keys"]
        assert set(keys) == {"api", "ui"}, keys
        assert keys["api"].startswith("implement-tests:api:")
        assert keys["ui"].startswith("implement-tests:ui:")
        assert keys["api"] != keys["ui"]
    finally:
        conn.close()


def test_idempotency_keys_stable_across_reruns(tmp_path: Path) -> None:
    """Re-running implement on the same work item over the same approved
    surfaces yields identical work-unit keys — a *stable identity*, defined by
    the approved surfaces and not the per-run ULID.

    The key does NOT itself dedup: dedup is owned by the coverage ledger
    (#320), and only *post-apply* — a pre-apply rerun deliberately REGENERATES
    (`test_unapplied_patch_leaves_ledger_empty_and_regenerates`, the Codex P1
    "no ghost coverage" guard). This test pins that the rerun here regenerates
    by design AND carries the same identity key, proving the key is a stable
    handle independent of the regenerate-vs-noop decision."""
    paths = _runtime(tmp_path)
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_two_api_one_ui(paths, work_item_id)
        events = EventLog(conn, paths)
        first = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        second = implement_tests_for_work_item(conn, paths, events, work_item_id=work_item_id)
        # Pre-apply rerun regenerates by design (Codex P1, no ghost coverage) —
        # so both runs reach the generate path and both carry keys.
        assert second.get("idempotent_noop") is not True
        first_keys = first["generated_v2"]["idempotency_keys"]
        second_keys = second["generated_v2"]["idempotency_keys"]
        assert first_keys and first_keys == second_keys, (first_keys, second_keys)
    finally:
        conn.close()


# ---- #362: conflict detection (disjoint output paths) --------------------

def test_no_collision_when_units_own_disjoint_paths() -> None:
    """Disjoint output sets across units → no conflict. This is the steady
    state: API units write tests/api/**, UI units write tests/ui/**."""
    specs = [
        _spec("c1", "tests/api/a.spec.ts"),
        _spec("c2", "tests/api/b.spec.ts"),
        _spec("c3", "tests/ui/x.spec.ts"),
    ]
    assert _detect_path_collisions(specs) == []


def test_collision_detected_when_two_units_claim_same_path() -> None:
    """Two units claiming the same output path is a conflict — returned sorted
    and de-duplicated so the guard's error message is deterministic. This pins
    the safety net for a future finer-grained (per-feature) fan-out."""
    specs = [
        _spec("c1", "tests/api/dup.spec.ts"),
        _spec("c2", "tests/api/dup.spec.ts"),  # same path, different unit
        _spec("c3", "tests/ui/z.spec.ts"),
        _spec("c4", "tests/ui/z.spec.ts"),      # second collision
    ]
    assert _detect_path_collisions(specs) == [
        "tests/api/dup.spec.ts",
        "tests/ui/z.spec.ts",
    ]


def test_collision_ignores_blank_paths() -> None:
    """Specs with no relative_path do not count as colliding with each other —
    only real, repeated output paths are conflicts."""
    assert _detect_path_collisions([_spec("c1", ""), _spec("c2", "")]) == []
