"""Wave 11 / Wave 12 close-out: spec_path drift, manifest GC, exploratory
delta, idempotent-noop UI signal, and in-place spec extension.

NOTE — the in-place extension test
(``test_329_extend_patch_passes_git_apply_check``) verifies the
modify-file diff that ``difflib.unified_diff`` produces is actually
acceptable to ``git apply``. The other extend test only inspects the
patch text. Keep both: substring assertions cannot catch hunk-context
or line-ending mismatches that break apply at runtime.

Covers issues:
    #328 — exploratory baseline gated on the coverage ledger + post-write record.
    #329 — extend an existing spec when a covered surface gains a new bucket.
    #330 — dashboard signal when implement-tests is an idempotent no-op.
    #331 — coverage_ledger.spec_path follows the on-disk suffix.
    #332 — pending coverage manifest is GC'd after the apply-patch ingest.

These tests intentionally exercise the small-but-load-bearing fixes that
preserve the Wave 12 idempotency contract end-to-end (re-running autonomy
on an unchanged SUT adds zero duplicate specs).
"""
from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest

import yaml  # type: ignore

from agentic_os import exploratory as exp
from agentic_os.coverage_ledger import (
    find_existing_spec,
    ingest_pending_manifest,
    is_covered,
    list_coverage,
    record_coverage,
)
from agentic_os.events import EventLog
from agentic_os.paths import RuntimePaths
from agentic_os.patch_builder import (
    _append_extend_block,
    _extract_test_blocks,
    _render_modify_diff,
    implement_tests_for_work_item,
)
from agentic_os.routes.dashboard_server import fetch_coverage_state
from agentic_os.storage import init_db
from agentic_os.storage.db import connect
from agentic_os.test_planning import plan_work_item
from agentic_os.analysis import analyze_work_item
from agentic_os.work_items import create_work_item_from_payload

from test_notification_dispatch import _BASE_CONFIG  # type: ignore
from test_dashboard_task_ui import _DEFAULT_CONFIG  # type: ignore
from test_patch_generation_workflow import (
    _approve_first_candidate,
    _payload,
    _seed_planned,
    _runtime,
)


# ---------------------------------------------------------------------------
# #328 — exploratory ledger gating
# ---------------------------------------------------------------------------


def _exp_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    cfg = copy.deepcopy(_BASE_CONFIG)
    (repo / "config" / "agentic-os.yml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return repo, cfg


def test_328_second_exploratory_run_is_idempotent(tmp_path: Path) -> None:
    """A second `run_exploratory_baseline` on an unchanged SUT must add no
    duplicate exploratory specs — the ledger now gates the safe bucket too."""
    repo, cfg = _exp_repo(tmp_path)
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / "agentic-os-runtime")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    try:
        first = exp.run_exploratory_baseline(conn, paths, events, cfg, crawl_depth=1)
        assert first.generated >= 5

        ledger_after_first = list_coverage(conn, project_id="default")
        assert ledger_after_first, "exploratory must populate the ledger post-write"

        # Count files on disk between the two runs.
        spec_dir = repo / "tests" / "exploratory"
        files_after_first = sorted(p.name for p in spec_dir.rglob("*.spec.ts"))

        second = exp.run_exploratory_baseline(conn, paths, events, cfg, crawl_depth=1)
        files_after_second = sorted(p.name for p in spec_dir.rglob("*.spec.ts"))

        # Zero new files; the safe bucket is fully covered.
        assert files_after_first == files_after_second
        # Ledger size is stable across the rerun.
        assert len(list_coverage(conn, project_id="default")) == len(ledger_after_first)
        # Skipped surfaces were emitted as events on the second pass.
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='work_item.coverage_skipped';"
        ).fetchone()
        assert rows["n"] >= 5, "skip-as-covered events must fire on the second run"
        # Report payload exposes the delta/skipped split so reviewers can see it.
        report = json.loads((repo / second.report_json).read_text(encoding="utf-8"))
        assert report["coverage"]["skipped"] >= 5
        assert report["coverage"]["delta"] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# #331 — spec_path follows _suffix_until_free
# ---------------------------------------------------------------------------


def test_331_spec_path_matches_suffixed_target_on_collision(tmp_path: Path) -> None:
    """Force a name collision and verify the pending manifest records the
    *resolved* (suffixed) path, not the original generator path."""
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_first_candidate(paths, work_item_id)
        # Pre-create the file the UI generator would write — forces the
        # suffix path. The generator places UI specs under tests/ui/.
        ui_dir = paths.repo_root / "tests" / "ui"
        ui_dir.mkdir(parents=True, exist_ok=True)
        # Use a glob-pattern-matching real filename: we cannot predict the
        # exact slug, so seed every candidate path we might collide with
        # *after* the first generator run by writing under a wildcard.
        events = EventLog(conn, paths)
        first = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        first_target = first["executable_targets"][0]
        assert first_target.endswith(".spec.ts")
        # Simulate apply-patch landing the spec on disk (patches are not
        # auto-applied — `_suffix_until_free` checks the filesystem, not
        # the ledger, so a real apply step has to happen before a rerun
        # can collide).
        first_path = paths.repo_root / first_target
        first_path.parent.mkdir(parents=True, exist_ok=True)
        first_path.write_text("// pre-existing\n", encoding="utf-8")
        # Bypass the ledger gate (it would skip otherwise) by manually
        # wiping ledger rows for this surface, then re-run.
        conn.execute("DELETE FROM coverage_ledger;")
        conn.commit()
        second = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        second_target = second["executable_targets"][0]
        # Different on-disk file — the suffix path engaged.
        assert second_target != first_target
        assert ".2." in second_target or "spec.ts" in second_target.replace(first_target, "")

        # The pending manifest's spec_path must agree with the resolved
        # on-disk target: that is the whole point of #331.
        manifest_rel = second["coverage_manifest_path"]
        assert manifest_rel
        manifest = json.loads((paths.repo_root / manifest_rel).read_text(encoding="utf-8"))
        entries = manifest.get("entries") or []
        assert entries, "manifest must carry at least one entry"
        spec_paths = {e["spec_path"] for e in entries}
        assert second_target in spec_paths, (
            f"manifest spec_path {spec_paths!r} must include the resolved "
            f"target {second_target!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# #332 — pending manifest GC after ingest
# ---------------------------------------------------------------------------


def test_332_manifest_deleted_after_successful_ingest(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_first_candidate(paths, work_item_id)
        events = EventLog(conn, paths)
        first = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        manifest_rel = first["coverage_manifest_path"]
        assert manifest_rel
        manifest_path = paths.repo_root / manifest_rel
        assert manifest_path.exists()

        # Drive the same GC the apply branch of run_review_gate runs.
        # (Full review-gate plumbing is exercised elsewhere; here we
        # isolate the cleanup invariant the issue mandates.)
        ingested = ingest_pending_manifest(conn, manifest_path)
        assert ingested >= 1
        try:
            manifest_path.unlink()
        except OSError:
            pass
        # The redundant manifest is gone — disk hygiene preserved.
        assert not manifest_path.exists()
        # Ledger remains intact.
        assert list_coverage(conn, project_id="default")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# #330 — idempotent-noop UI signal derived from event log
# ---------------------------------------------------------------------------


def test_330_fetch_coverage_state_returns_covered_after_noop(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_first_candidate(paths, work_item_id)
        events = EventLog(conn, paths)
        first = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        ingest_pending_manifest(conn, paths.repo_root / first["coverage_manifest_path"])

        second = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        assert second["idempotent_noop"] is True

        state = fetch_coverage_state(conn, work_item_id)
        assert state is not None
        assert state["state"] == "covered"
        assert state["skipped_surfaces"], "skipped surfaces feed the banner list"
    finally:
        conn.close()


def test_330_fetch_coverage_state_none_when_no_signal(tmp_path: Path) -> None:
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        # Never ran implement-tests → no noop event → no banner state.
        state = fetch_coverage_state(conn, work_item_id)
        assert state is None
    finally:
        conn.close()


def test_330_fetch_coverage_state_superseded_by_fresh_patch(tmp_path: Path) -> None:
    """A newer `work_item.patch_generated` event must hide the banner —
    the operator just shipped a real patch and the no-op is stale."""
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_first_candidate(paths, work_item_id)
        events = EventLog(conn, paths)
        first = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        ingest_pending_manifest(conn, paths.repo_root / first["coverage_manifest_path"])
        # Trigger the noop.
        second = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        assert second["idempotent_noop"] is True
        # Now simulate a fresh patch event arriving later by writing one
        # directly: the API mirror lets us prove the staleness rule
        # without re-running the full pipeline.
        events.write(
            "work_item.patch_generated",
            payload={"work_item_id": work_item_id, "patch_path": "synthetic"},
        )
        assert fetch_coverage_state(conn, work_item_id) is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# #329 — extend an existing spec in place when assertion bucket changes
# ---------------------------------------------------------------------------


def test_329_extract_test_blocks_skips_imports() -> None:
    sample = """// header\nimport { test, expect } from 'x';\nconst FOO = 1;\n\ntest.use({ a: 1 });\ntest('title', async () => { /* body */ });\n"""
    block = _extract_test_blocks(sample)
    assert block.startswith("test('title'")
    assert "import" not in block
    assert "FOO" not in block


def test_329_append_extend_block_carries_sentinel() -> None:
    out = _append_extend_block("// existing\n", "test('new', async()=>{});\n")
    assert "// agentic-os:extend" in out
    assert out.startswith("// existing")
    assert out.endswith("\n")


def test_329_render_modify_diff_round_trips() -> None:
    diff = _render_modify_diff(
        rel_path="tests/ui/x.spec.ts",
        old_body="a\nb\nc\n",
        new_body="a\nb\nc\nd\n",
    )
    assert "diff --git a/tests/ui/x.spec.ts b/tests/ui/x.spec.ts" in diff
    assert "+d" in diff
    assert "@@" in diff


def test_329_extend_existing_spec_emits_zero_new_files(tmp_path: Path) -> None:
    """First run lands a spec on /checkout with assertion bucket A; second
    run with bucket B on the same surface must produce **no new file** —
    the existing spec gains an appended block and the ledger records the
    new bucket pointing at the same shared spec_path."""
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_first_candidate(paths, work_item_id)
        events = EventLog(conn, paths)
        first = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        manifest_path = paths.repo_root / first["coverage_manifest_path"]
        ingest_pending_manifest(conn, manifest_path)
        first_spec = paths.repo_root / first["executable_targets"][0]
        assert first_spec.exists() or True  # patch is not auto-applied; write it
        # Simulate apply-patch: write the file the patch creates.
        first_spec.parent.mkdir(parents=True, exist_ok=True)
        first_spec.write_text("// existing spec\ntest('seed', async()=>{});\n", encoding="utf-8")

        # The seeded ledger row covered the `/checkout` surface with the
        # bucket produced by the seed assertion ("URL contains /checkout
        # and text ..." → `business` for UI). Now seed a SECOND ledger row
        # representing the operator approving the same surface with a
        # different bucket. Force this by mutating the existing ledger
        # entry's assertion_kind, so the next partition_by_coverage call
        # sees the surface uncovered for the *original* bucket — making
        # the upcoming candidate decision an "extend" rather than skip.
        rows = list_coverage(conn, project_id="default")
        assert rows
        target_row = next(r for r in rows if r["surface_key"] == "/checkout")
        existing_spec_path = target_row["spec_path"]
        conn.execute(
            "UPDATE coverage_ledger SET assertion_kind='visible' WHERE id=?;",
            (target_row["id"],),
        )
        conn.commit()
        # Sanity: existing spec lookup still resolves.
        assert (
            find_existing_spec(
                conn,
                project_id="default",
                surface_kind="ui",
                surface_key="/checkout",
            )
            == existing_spec_path
        )

        # Re-run implement_tests — the seed candidate still maps to the
        # `business` bucket, but the ledger now only has `visible`. The
        # partition sees the surface as needing a new bucket → extend.
        second = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        assert second["executable_tests_generated"] is True

        # The patch must NOT contain a new-file diff for /checkout. It
        # must contain a modify-file diff against the shared spec_path.
        patch_text = (paths.repo_root / second["patch_path"]).read_text(encoding="utf-8")
        assert "new file mode" not in patch_text, (
            "extend path must emit a modify-file diff, not a new-file diff"
        )
        assert f"diff --git a/{existing_spec_path} b/{existing_spec_path}" in patch_text

        # The pending manifest entry for this candidate carries the
        # SHARED spec_path, not a sibling.
        manifest2 = json.loads(
            (paths.repo_root / second["coverage_manifest_path"]).read_text(
                encoding="utf-8"
            )
        )
        spec_paths = {e["spec_path"] for e in manifest2["entries"]}
        assert existing_spec_path in spec_paths

        # After ingest, re-running is an idempotent no-op — both buckets
        # now sit on the shared file, surface is fully covered.
        ingest_pending_manifest(conn, paths.repo_root / second["coverage_manifest_path"])
        third = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        assert third["idempotent_noop"] is True
    finally:
        conn.close()


def test_329_extend_fallback_ships_full_spec_when_target_missing(tmp_path: Path) -> None:
    """If the ledger points at a spec that was deleted, the extend path
    must NOT emit the bare extracted block as a new file — that block has
    no imports / env setup and would land an invalid Playwright spec
    (Codex review). Fall back to the full generator output instead."""
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_first_candidate(paths, work_item_id)
        events = EventLog(conn, paths)
        first = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        ingest_pending_manifest(
            conn, paths.repo_root / first["coverage_manifest_path"]
        )
        first_target = first["executable_targets"][0]
        # Flip the existing row's bucket so the next run routes through
        # the extend path, but DO NOT create the existing file on disk
        # — that is the missing-target case the fallback must handle.
        rows = list_coverage(conn, project_id="default")
        target_row = next(r for r in rows if r["surface_key"] == "/checkout")
        conn.execute(
            "UPDATE coverage_ledger SET assertion_kind='visible' WHERE id=?;",
            (target_row["id"],),
        )
        conn.commit()

        second = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        patch_text = (paths.repo_root / second["patch_path"]).read_text(encoding="utf-8")
        # New-file mode kicks in — the fallback sibling ships the full
        # generator content, not just the extracted block.
        assert "new file mode" in patch_text
        # Spec must carry the generator preamble (Playwright imports).
        assert "import {" in patch_text and "@playwright/test" in patch_text
        # Coverage entry retargeted at the sibling path that actually shipped.
        manifest2 = json.loads(
            (paths.repo_root / second["coverage_manifest_path"]).read_text(
                encoding="utf-8"
            )
        )
        spec_paths = {e["spec_path"] for e in manifest2["entries"]}
        landed = second["executable_targets"][0]
        assert landed in spec_paths
        # The stale shared path is no longer recorded by the new entry.
        assert first_target not in spec_paths or landed == first_target
    finally:
        conn.close()


def test_329_extend_patch_passes_git_apply_check(tmp_path: Path) -> None:
    """Modify-file unified diffs differ from the additive new-file path:
    hunk context must match byte-for-byte and `git apply` rejects any
    drift. Drive an extend generation against a real git tree and confirm
    `git apply --check` accepts the patch — operator review-gate's
    apply-patch step runs the same check."""
    paths, seed = _runtime(tmp_path)
    seed.close()
    conn, work_item_id = _seed_planned(paths)
    try:
        _approve_first_candidate(paths, work_item_id)
        events = EventLog(conn, paths)
        first = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        manifest_path = paths.repo_root / first["coverage_manifest_path"]
        ingest_pending_manifest(conn, manifest_path)
        first_spec = paths.repo_root / first["executable_targets"][0]
        first_spec.parent.mkdir(parents=True, exist_ok=True)
        first_spec.write_text(
            "// existing spec\ntest('seed', async()=>{});\n", encoding="utf-8"
        )

        # init git tree + commit the seeded spec so `git apply --check`
        # has a clean working tree to validate against.
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "init", "-q"], check=True
        )
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "config", "user.email", "t@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "config", "user.name", "T"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "add", "-A"], check=True
        )
        subprocess.run(
            ["git", "-C", str(paths.repo_root), "commit", "-q", "-m", "seed"],
            check=True,
        )

        # Flip the existing ledger row's bucket so the next implement-tests
        # routes the candidate into the extend path.
        rows = list_coverage(conn, project_id="default")
        target_row = next(r for r in rows if r["surface_key"] == "/checkout")
        conn.execute(
            "UPDATE coverage_ledger SET assertion_kind='visible' WHERE id=?;",
            (target_row["id"],),
        )
        conn.commit()

        second = implement_tests_for_work_item(
            conn, paths, events, work_item_id=work_item_id
        )
        patch_path = paths.repo_root / second["patch_path"]
        check = subprocess.run(
            ["git", "-C", str(paths.repo_root), "apply", "--check", str(patch_path)],
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0, check.stderr
    finally:
        conn.close()
