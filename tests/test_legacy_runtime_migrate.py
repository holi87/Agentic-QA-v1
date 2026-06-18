"""Tests for the legacy-runtime warn + migrate flow (issue #142).

Two surfaces:

- ``cmd_doctor`` reports the legacy / visible runtime co-existence
  and adds a warning so an operator does not silently debug on the
  wrong tree.
- ``cmd_migrate-runtime`` consolidates onto ``agentic-os-runtime/``
  with explicit dry-run + force semantics, and refuses to overwrite
  a populated visible state DB without ``--force``.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import List

import pytest

from agentic_os.cli import cmd_doctor, cmd_migrate_runtime


def _run(func, repo_root: Path, args: List[str], *, json_output: bool = True) -> tuple[int, dict]:
    """Invoke a cli.cmd_* helper, capture stdout, parse JSON when requested."""
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = func(repo_root, args, json_output=json_output)
    finally:
        sys.stdout = real_stdout
    text = buf.getvalue()
    payload = json.loads(text) if json_output and text.strip().startswith("{") else {"_raw": text}
    return rc, payload


def _seed_minimal_config(repo: Path) -> None:
    """Doctor needs a loadable config; tests here only care about the
    runtime-layout section, so seed the smallest valid YAML."""
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        """
runtime:
  root: agentic-os-runtime
  timezone: Europe/Warsaw
  max_parallel_tasks: 1
  heartbeat_seconds: 20
  lease_ttl_seconds: 60
  stale_lease_seconds: 90
  shutdown_grace_seconds: 5
  timeouts:
    default_seconds: 1800
    docker_seconds: 240
    test_seconds: 3600
    model_seconds: 1800
    report_seconds: 300
sut:
  root: .
  compose_file: docker-compose.yml
  compose_project_name: agentic-os-sut
  autostart: false
  healthcheck:
    command: ["true"]
    timeout_seconds: 30
    retries: 1
  test_runner: ./run-tests.sh
  install_shim_allowed: false
models:
  planner: { provider: claude, command: ["claude"], role: opus }
  implementer: { provider: claude, command: ["claude"], role: sonnet }
  reviewer: { provider: codex, command: ["codex"], role: codex }
dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: false
paths:
  reports: reports
  bugs: bugs
  evidence: evidence
  prompts: .qualitycat/prompts
reports:
  copy_reports_script: scripts/copy-reports.sh
  extract_last_run_script: scripts/extract-last-run.sh
  build_summary_script: scripts/build-summary.sh
  require_reports_on_failure: true
gates:
  known_bugs_fail_exit: true
  assertion_changes_require_decision: true
  exact_spec_failure_opens_bug: true
  require_functional_area_tag: true
  require_lifecycle_tag: true
  infrastructure_exit_code: 2
""".lstrip(),
        encoding="utf-8",
    )


def _seed_legacy(repo: Path) -> Path:
    legacy = repo / ".agentic-os"
    legacy.mkdir()
    (legacy / "state.db").write_bytes(b"SQLITE-FAKE")
    (legacy / "events").mkdir()
    (legacy / "events" / "log.jsonl").write_text("{}\n", encoding="utf-8")
    return legacy


def _seed_visible(repo: Path, *, with_db: bool = False) -> Path:
    visible = repo / "agentic-os-runtime"
    visible.mkdir()
    if with_db:
        (visible / "state.db").write_bytes(b"SQLITE-FAKE-VISIBLE")
    return visible


# ---------------------------------------------------------------------------
# Doctor warnings
# ---------------------------------------------------------------------------


def test_doctor_warns_when_both_runtimes_exist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_minimal_config(repo)
    _seed_legacy(repo)
    _seed_visible(repo)
    rc, payload = _run(cmd_doctor, repo, [])
    assert rc == 0, payload  # warnings are not blocking
    warnings = payload["runtime"]["warnings"]
    assert any("migrate-runtime" in w for w in warnings), warnings
    assert payload["runtime"]["legacy_exists"]
    assert payload["runtime"]["canonical_exists"]


def test_doctor_warns_when_only_legacy_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_minimal_config(repo)
    _seed_legacy(repo)
    rc, payload = _run(cmd_doctor, repo, [])
    assert rc == 0
    warnings = payload["runtime"]["warnings"]
    assert any("legacy" in w and "migrate-runtime" in w for w in warnings), warnings


def test_doctor_quiet_when_only_visible_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_minimal_config(repo)
    _seed_visible(repo)
    rc, payload = _run(cmd_doctor, repo, [])
    assert rc == 0
    assert payload["runtime"]["warnings"] == []


# ---------------------------------------------------------------------------
# migrate-runtime
# ---------------------------------------------------------------------------


def test_migrate_runtime_no_op_when_legacy_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    rc, payload = _run(cmd_migrate_runtime, repo, [])
    assert rc == 0
    assert payload["status"] == "nothing-to-migrate"


def test_migrate_runtime_dry_run_changes_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_legacy(repo)
    rc, payload = _run(cmd_migrate_runtime, repo, ["--dry-run"])
    assert rc == 0
    assert payload["status"] == "dry-run"
    # Filesystem must be untouched.
    assert (repo / ".agentic-os" / "state.db").exists()
    assert not (repo / "agentic-os-runtime").exists()
    # And it must report the planned ops in order.
    op_kinds = [step["op"] for step in payload["actions"]]
    assert op_kinds[-2:] == ["copy_tree", "archive_legacy"]


def test_migrate_runtime_moves_legacy_to_visible(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_legacy(repo)
    rc, payload = _run(cmd_migrate_runtime, repo, [])
    assert rc == 0
    assert payload["status"] == "migrated"

    visible = repo / "agentic-os-runtime"
    assert visible.exists()
    assert (visible / "state.db").read_bytes() == b"SQLITE-FAKE"
    assert (visible / "events" / "log.jsonl").exists()

    # Legacy is gone but archived next to the runtime.
    assert not (repo / ".agentic-os").exists()
    archives = [p for p in repo.iterdir() if p.name.startswith(".agentic-os.legacy-")]
    assert len(archives) == 1
    assert (archives[0] / "state.db").read_bytes() == b"SQLITE-FAKE"


def test_migrate_runtime_archives_stub_visible_runtime(tmp_path: Path) -> None:
    """`init` creates an empty visible runtime even when legacy exists.

    The migrator must not error on that stub — it archives it,
    consolidates the legacy data into the canonical path, and the
    operator ends up with one source of truth.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_legacy(repo)
    _seed_visible(repo, with_db=False)
    rc, payload = _run(cmd_migrate_runtime, repo, [])
    assert rc == 0, payload
    assert payload["status"] == "migrated"
    assert (repo / "agentic-os-runtime" / "state.db").read_bytes() == b"SQLITE-FAKE"
    pre = [p for p in repo.iterdir() if p.name.startswith("agentic-os-runtime.pre-migrate-")]
    assert len(pre) == 1


def test_migrate_runtime_refuses_when_both_have_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_legacy(repo)
    _seed_visible(repo, with_db=True)
    rc, payload = _run(cmd_migrate_runtime, repo, [])
    assert rc == 2
    assert payload["status"] == "blocked"
    # Both trees still in place — operator picks the winner.
    assert (repo / ".agentic-os" / "state.db").exists()
    assert (repo / "agentic-os-runtime" / "state.db").exists()


def test_migrate_runtime_force_clobbers_visible_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_legacy(repo)
    _seed_visible(repo, with_db=True)
    rc, payload = _run(cmd_migrate_runtime, repo, ["--force"])
    assert rc == 0
    assert payload["status"] == "migrated"
    assert (repo / "agentic-os-runtime" / "state.db").read_bytes() == b"SQLITE-FAKE"
    # The previous visible state is archived under .clobbered-* so the
    # operator can still recover it if --force was a mistake.
    clobbered = [p for p in repo.iterdir() if p.name.startswith("agentic-os-runtime.clobbered-")]
    assert len(clobbered) == 1
    assert (clobbered[0] / "state.db").read_bytes() == b"SQLITE-FAKE-VISIBLE"


def test_dry_run_force_lists_force_archive_action(tmp_path: Path) -> None:
    """Codex review on #142: `--dry-run --force` must surface the
    destructive `force_archive_visible` step. Without this, operators
    cannot reliably preview the high-risk path before running it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_legacy(repo)
    _seed_visible(repo, with_db=True)
    rc, payload = _run(cmd_migrate_runtime, repo, ["--dry-run", "--force"])
    assert rc == 0
    assert payload["status"] == "dry-run"
    op_kinds = [step["op"] for step in payload["actions"]]
    assert "force_archive_visible" in op_kinds, op_kinds
    # And the destructive step must come BEFORE the copy so the operator
    # reads the plan top-to-bottom in execution order.
    assert op_kinds.index("force_archive_visible") < op_kinds.index("copy_tree")
    # Filesystem still untouched.
    assert (repo / "agentic-os-runtime" / "state.db").exists()
    assert (repo / ".agentic-os" / "state.db").exists()


def test_doctor_suppresses_warning_when_legacy_is_configured_root(tmp_path: Path) -> None:
    """Codex review on #142: operators may explicitly set
    ``runtime.root: .agentic-os`` in their config. In that case the
    legacy path is the intended runtime — `doctor` must not push them
    toward `migrate-runtime` away from their own configured layout.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_legacy(repo)
    # Write a config that explicitly points at the legacy root.
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        """
runtime:
  root: .agentic-os
  timezone: Europe/Warsaw
  max_parallel_tasks: 1
  heartbeat_seconds: 20
  lease_ttl_seconds: 60
  stale_lease_seconds: 90
  shutdown_grace_seconds: 5
  timeouts:
    default_seconds: 1800
    docker_seconds: 240
    test_seconds: 3600
    model_seconds: 1800
    report_seconds: 300
sut:
  root: .
  compose_file: docker-compose.yml
  compose_project_name: agentic-os-sut
  autostart: false
  healthcheck:
    command: ["true"]
    timeout_seconds: 30
    retries: 1
  test_runner: ./run-tests.sh
  install_shim_allowed: false
models:
  planner: { provider: claude, command: ["claude"], role: opus }
  implementer: { provider: claude, command: ["claude"], role: sonnet }
  reviewer: { provider: codex, command: ["codex"], role: codex }
dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: false
paths:
  reports: reports
  bugs: bugs
  evidence: evidence
  prompts: .qualitycat/prompts
reports:
  copy_reports_script: scripts/copy-reports.sh
  extract_last_run_script: scripts/extract-last-run.sh
  build_summary_script: scripts/build-summary.sh
  require_reports_on_failure: true
gates:
  known_bugs_fail_exit: true
  assertion_changes_require_decision: true
  exact_spec_failure_opens_bug: true
  require_functional_area_tag: true
  require_lifecycle_tag: true
  infrastructure_exit_code: 2
""".lstrip(),
        encoding="utf-8",
    )
    rc, payload = _run(cmd_doctor, repo, [])
    assert rc == 0
    assert payload["runtime"]["legacy_is_configured"] is True
    assert payload["runtime"]["warnings"] == [], payload["runtime"]["warnings"]
