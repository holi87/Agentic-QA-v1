"""Issue #319 (Wave 12) — persistent per-SUT/project coverage ledger.

The ledger records every surface a generator covered (route/endpoint +
assertion kind), the spec file that covers it, and the run/work item that
produced it. It answers "is surface X already covered?" — the foundation the
idempotent accumulation in #320 builds on.

These tests pin:
- canonical surface-key derivation for API and UI plan items;
- record → query (hit) and unseen → query (miss);
- idempotent recording (re-generating a surface updates the pointer, no dup);
- project scoping (no cross-project bleed);
- the generator-emit recording helper used by patch_builder;
- the migration that introduces the `coverage_ledger` table.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_os.coverage_ledger import (
    build_coverage_entries,
    classify_assertion,
    ingest_pending_manifest,
    is_covered,
    list_coverage,
    partition_by_coverage,
    record_coverage,
    record_generated_coverage,
    surface_key,
    write_pending_manifest,
)
from agentic_os.generators.api import GeneratedTest, generate_api_tests
from agentic_os.generators.ui import generate_ui_tests
from agentic_os.plan_v2 import PlanItem
from agentic_os.projects import register_project
from agentic_os.storage.db import (
    SCHEMA_VERSION,
    assert_db_healthy,
    current_version,
    init_db,
)


def _api_item(candidate_id: str = "API-1", method: str = "GET", path: str = "/users") -> PlanItem:
    return PlanItem(
        candidate_id=candidate_id,
        title=f"{method} {path}",
        test_type="api",
        priority="P2",
        decision="generate_now",
        expected_assertion="response status 200",
        source_refs=[f"openapi.json#{path}/{method.lower()}"],
        target_method=method,
        target_path=path,
    )


def _ui_item(candidate_id: str = "UI-1", page: str = "/login") -> PlanItem:
    return PlanItem(
        candidate_id=candidate_id,
        title=f"visit {page}",
        test_type="ui",
        priority="P2",
        decision="generate_now",
        expected_assertion='text "Dashboard" is visible',
        source_refs=[f"sitemap#{page}"],
        target_page=page,
    )


# --- surface-key derivation -------------------------------------------------

def test_surface_key_api_is_method_and_path() -> None:
    assert surface_key("api", target_method="get", target_path="/users") == "GET /users"


def test_surface_key_ui_is_target_page() -> None:
    assert surface_key("ui", target_page="/login") == "/login"


def test_classify_assertion_buckets_are_deterministic() -> None:
    assert classify_assertion("api", "response status is 200") == "status"
    assert classify_assertion("ui", "the heading is visible") == "visible"


# --- record / query ---------------------------------------------------------

def test_record_then_query_returns_hit(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        record_coverage(
            conn,
            project_id="default",
            surface_kind="api",
            surface_key="GET /users",
            assertion_kind="status",
            spec_path="tests/api/api-1-get-users.spec.ts",
            candidate_id="API-1",
        )
        assert is_covered(conn, project_id="default", surface_kind="api", surface_key="GET /users")
    finally:
        conn.close()


def test_unseen_surface_returns_miss(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        record_coverage(
            conn,
            project_id="default",
            surface_kind="api",
            surface_key="GET /users",
            assertion_kind="status",
            spec_path="tests/api/api-1.spec.ts",
        )
        assert not is_covered(
            conn, project_id="default", surface_kind="api", surface_key="POST /orders"
        )
    finally:
        conn.close()


def test_list_coverage_reports_every_surface_and_spec(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        record_coverage(
            conn,
            project_id="default",
            surface_kind="api",
            surface_key="GET /users",
            assertion_kind="status",
            spec_path="tests/api/users.spec.ts",
        )
        record_coverage(
            conn,
            project_id="default",
            surface_kind="ui",
            surface_key="/login",
            assertion_kind="visible",
            spec_path="tests/ui/login.spec.ts",
        )
        rows = list_coverage(conn, project_id="default")
        pairs = {(r["surface_key"], r["spec_path"]) for r in rows}
        assert ("GET /users", "tests/api/users.spec.ts") in pairs
        assert ("/login", "tests/ui/login.spec.ts") in pairs
    finally:
        conn.close()


def test_recording_same_surface_twice_is_idempotent(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        record_coverage(
            conn,
            project_id="default",
            surface_kind="api",
            surface_key="GET /users",
            assertion_kind="status",
            spec_path="tests/api/old.spec.ts",
            run_id="RUN-1",
        )
        record_coverage(
            conn,
            project_id="default",
            surface_kind="api",
            surface_key="GET /users",
            assertion_kind="status",
            spec_path="tests/api/new.spec.ts",
            run_id="RUN-2",
        )
        rows = list_coverage(conn, project_id="default")
        assert len(rows) == 1
        # Re-generation updates the pointer to the latest spec/run.
        assert rows[0]["spec_path"] == "tests/api/new.spec.ts"
        assert rows[0]["run_id"] == "RUN-2"
    finally:
        conn.close()


def test_coverage_is_project_scoped(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        register_project(conn, project_id="shop", name="shop", sut_root="./shop")
        record_coverage(
            conn,
            project_id="default",
            surface_kind="api",
            surface_key="GET /users",
            assertion_kind="status",
            spec_path="tests/api/users.spec.ts",
        )
        # The same surface is unseen for a different project — no bleed.
        assert not is_covered(
            conn, project_id="shop", surface_kind="api", surface_key="GET /users"
        )
        assert list_coverage(conn, project_id="shop") == []
    finally:
        conn.close()


# --- idempotent gating (#320) ------------------------------------------------

def test_partition_by_coverage_splits_covered_from_delta(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        record_coverage(
            conn,
            project_id="default",
            surface_kind="api",
            surface_key="GET /users",
            assertion_kind="status",
            spec_path="tests/api/users.spec.ts",
        )
        covered = _api_item("API-1", "GET", "/users")  # bucket -> status (already covered)
        fresh = _api_item("API-2", "POST", "/orders")
        delta, skipped = partition_by_coverage(
            conn, project_id="default", plan_items=[covered, fresh]
        )
        assert [i.candidate_id for i in delta] == ["API-2"]
        assert len(skipped) == 1
        assert skipped[0]["surface_key"] == "GET /users"
        assert skipped[0]["reason"] == "already_covered"
    finally:
        conn.close()


def test_partition_keeps_items_without_a_derivable_surface(tmp_path: Path) -> None:
    """An item the ledger cannot key (no target) cannot be gated — it stays in
    the delta so generation still has a chance to validate/reject it."""
    conn = init_db(tmp_path / "state.db")
    try:
        bad = PlanItem(
            candidate_id="API-1",
            title="no target",
            test_type="api",
            priority="P2",
            decision="generate_now",
            expected_assertion="response status 200",
            source_refs=["openapi.json#x"],
            target_method="GET",
            target_path=None,  # no surface key derivable
        )
        delta, skipped = partition_by_coverage(
            conn, project_id="default", plan_items=[bad]
        )
        assert [i.candidate_id for i in delta] == ["API-1"]
        assert skipped == []
    finally:
        conn.close()


def test_partition_is_project_scoped(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        record_coverage(
            conn,
            project_id="default",
            surface_kind="api",
            surface_key="GET /users",
            assertion_kind="status",
            spec_path="tests/api/users.spec.ts",
        )
        register_project(conn, project_id="shop", name="shop", sut_root="./shop")
        item = _api_item("API-1", "GET", "/users")
        # Covered under `default`, but unseen for `shop` — must not be skipped.
        delta, skipped = partition_by_coverage(
            conn, project_id="shop", plan_items=[item]
        )
        assert [i.candidate_id for i in delta] == ["API-1"]
        assert skipped == []
    finally:
        conn.close()


# --- generator-emit recording helper ----------------------------------------

def test_record_generated_coverage_ingests_a_generation_run(tmp_path: Path) -> None:
    """After a generation run the ledger lists every covered surface + spec."""
    conn = init_db(tmp_path / "state.db")
    try:
        items = [_api_item("API-1", "GET", "/users"), _ui_item("UI-1", "/login")]
        api_specs = generate_api_tests([i for i in items if i.test_type == "api"])
        ui_specs = generate_ui_tests([i for i in items if i.test_type == "ui"])
        record_generated_coverage(
            conn,
            project_id="default",
            plan_items=items,
            generated_tests=list(api_specs) + list(ui_specs),
            run_id="RUN-1",
            work_item_id="TASK-1",
        )
        rows = list_coverage(conn, project_id="default")
        surfaces = {(r["surface_kind"], r["surface_key"]) for r in rows}
        assert surfaces == {("api", "GET /users"), ("ui", "/login")}
        assert all(r["spec_path"] for r in rows)
        assert all(r["run_id"] == "RUN-1" for r in rows)
    finally:
        conn.close()


# --- pending manifest / apply-time ingest -----------------------------------

def test_build_coverage_entries_derives_surface_and_assertion() -> None:
    items = [_api_item("API-1", "GET", "/users"), _ui_item("UI-1", "/login")]
    api_specs = [
        type("G", (), {"candidate_id": "API-1", "relative_path": "tests/api/u.spec.ts"})()
    ]
    ui_specs = [
        type("G", (), {"candidate_id": "UI-1", "relative_path": "tests/ui/l.spec.ts"})()
    ]
    entries = build_coverage_entries(
        items,
        api_specs + ui_specs,
        project_id="default",
        work_item_id="TASK-1",
        run_id="RUN-1",
    )
    by_key = {(e["surface_kind"], e["surface_key"]): e for e in entries}
    assert by_key[("api", "GET /users")]["assertion_kind"] == "status"
    assert by_key[("api", "GET /users")]["spec_path"] == "tests/api/u.spec.ts"
    assert by_key[("ui", "/login")]["spec_path"] == "tests/ui/l.spec.ts"
    # Every entry must be JSON-serialisable end-to-end.
    json.dumps(entries)


def test_write_and_ingest_pending_manifest_round_trip(tmp_path: Path) -> None:
    """Codex P1 — ledger writes happen at apply, not at generation. The
    manifest is the durable hand-off between the two."""
    conn = init_db(tmp_path / "state.db")
    try:
        manifest = tmp_path / "patch.coverage.json"
        write_pending_manifest(
            manifest,
            [
                {
                    "project_id": "default",
                    "surface_kind": "api",
                    "surface_key": "GET /users",
                    "assertion_kind": "status",
                    "spec_path": "tests/api/u.spec.ts",
                    "candidate_id": "API-1",
                    "work_item_id": "TASK-1",
                    "run_id": "RUN-1",
                }
            ],
        )
        assert ingest_pending_manifest(conn, manifest) == 1
        assert is_covered(
            conn, project_id="default", surface_kind="api", surface_key="GET /users"
        )
        # Second ingest is a no-op — record_coverage is idempotent.
        assert ingest_pending_manifest(conn, manifest) == 1
        assert len(list_coverage(conn, project_id="default")) == 1
    finally:
        conn.close()


def test_ingest_pending_manifest_missing_file_returns_zero(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        assert ingest_pending_manifest(conn, tmp_path / "absent.json") == 0
    finally:
        conn.close()


# --- CLI query surface -------------------------------------------------------

def test_coverage_cli_lists_and_checks(tmp_path: Path, capsys) -> None:
    from agentic_os.cli import cmd_coverage, open_runtime

    (tmp_path / ".git").mkdir()
    conn = open_runtime(tmp_path)[0]
    record_coverage(
        conn,
        project_id="default",
        surface_kind="api",
        surface_key="GET /users",
        assertion_kind="status",
        spec_path="tests/api/users.spec.ts",
    )
    conn.close()

    rc = cmd_coverage(tmp_path, ["list"], json_output=True)
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)["coverage"]
    assert any(r["surface_key"] == "GET /users" for r in rows)

    rc = cmd_coverage(
        tmp_path, ["check", "--kind", "api", "--key", "GET /users"], json_output=True
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["covered"] is True

    rc = cmd_coverage(
        tmp_path, ["check", "--kind", "api", "--key", "POST /orders"], json_output=True
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["covered"] is False


# --- migration --------------------------------------------------------------

def test_fresh_install_has_coverage_ledger_table(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        assert current_version(conn) == SCHEMA_VERSION
        cols = {row[1] for row in conn.execute("PRAGMA table_info(coverage_ledger);").fetchall()}
        assert {"project_id", "surface_kind", "surface_key", "assertion_kind", "spec_path"} <= cols
        assert_db_healthy(conn)
    finally:
        conn.close()
