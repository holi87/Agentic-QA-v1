"""Config-driven SUT git bootstrap behavior."""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_os import sut_repo
from agentic_os.config import _validate
from agentic_os.events import EventLog
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


def _setup(tmp_path: Path) -> tuple[RuntimePaths, EventLog]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    (paths.repo_root / "sut").mkdir(parents=True, exist_ok=True)
    conn = init_db(paths.db)
    sut_repo._SKIPPED_NOTIFIED.clear()
    return paths, EventLog(conn, paths)


def _base_cfg() -> dict:
    return {
        "runtime": {
            "root": "agentic-os-runtime",
            "timezone": "UTC",
            "max_parallel_tasks": 1,
            "heartbeat_seconds": 10,
            "lease_ttl_seconds": 60,
            "stale_lease_seconds": 120,
            "shutdown_grace_seconds": 5,
            "timeouts": {
                "default_seconds": 60,
                "docker_seconds": 60,
                "test_seconds": 60,
                "model_seconds": 60,
                "report_seconds": 60,
            },
        },
        "sut": {
            "root": ".",
            "compose_file": None,
            "compose_project_name": "x",
            "autostart": False,
            "healthcheck": {"command": ["true"], "timeout_seconds": 1, "retries": 1},
            "test_runner": "pytest",
            "install_shim_allowed": False,
        },
        "models": {
            "planner": {"provider": "claude", "command": ["claude"], "role": "opus"},
            "implementer": {"provider": "claude", "command": ["claude"], "role": "sonnet"},
            "reviewer": {"provider": "codex", "command": ["codex"], "role": "codex"},
        },
        "dashboard": {"host": "127.0.0.1", "port": 8765, "enable_write_endpoints": False},
        "paths": {"reports": "reports", "bugs": "bugs", "evidence": "evidence", "prompts": "config/prompts"},
        "reports": {
            "copy_reports_script": "a",
            "extract_last_run_script": "b",
            "build_summary_script": "c",
            "require_reports_on_failure": True,
        },
        "gates": {
            "known_bugs_fail_exit": True,
            "assertion_changes_require_decision": True,
            "exact_spec_failure_opens_bug": True,
            "require_functional_area_tag": True,
            "require_lifecycle_tag": True,
            "infrastructure_exit_code": 2,
        },
    }


def test_config_accepts_well_formed_git_block() -> None:
    cfg = _base_cfg()
    cfg["git"] = {
        "enabled": True,
        "auto_init": True,
        "origin": "git@github.com:owner/sut.git",
        "origin_branch": "main",
        "auto_fetch": True,
        "auto_publish": False,
    }
    assert _validate(cfg) == []


def test_config_rejects_invalid_remote_url() -> None:
    cfg = _base_cfg()
    cfg["git"] = {"enabled": True, "origin": "file:///etc/passwd"}
    errs = _validate(cfg)
    assert any("git.origin" in e for e in errs)


def test_config_rejects_bad_branch_name() -> None:
    cfg = _base_cfg()
    cfg["git"] = {"enabled": True, "origin_branch": "a b"}
    errs = _validate(cfg)
    assert any("git.origin_branch" in e for e in errs)


def test_disabled_short_circuits_with_ok(tmp_path: Path) -> None:
    paths, events = _setup(tmp_path)
    r = sut_repo.git_ensure(
        paths, events, git_config={"enabled": False}, sut_root="sut"
    )
    assert r.ok is True
    assert r.ops == []
    assert "disabled" in r.summary


def test_missing_binary_short_circuits_with_ok(tmp_path: Path) -> None:
    paths, events = _setup(tmp_path)
    with patch("agentic_os.sut_repo.shutil.which", return_value=None):
        r = sut_repo.git_ensure(
            paths,
            events,
            git_config={"enabled": True, "auto_init": True},
            sut_root="sut",
        )
    assert r.ok is True
    assert r.ops == []
    assert "binary" in r.summary or "missing" in r.summary


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary required")
def test_fresh_sut_init_then_idempotent_rerun(tmp_path: Path) -> None:
    paths, events = _setup(tmp_path)
    cfg = {
        "enabled": True,
        "auto_init": True,
        "origin": "git@github.com:owner/sut.git",
    }
    r1 = sut_repo.git_ensure(paths, events, git_config=cfg, sut_root="sut")
    assert r1.ok is True
    init_ops = [o for o in r1.ops if o["op"] == "init"]
    assert init_ops and init_ops[0]["ok"]
    assert sut_repo.has_git_repo(paths.repo_root / "sut")

    # Rerun — no init op (already a repo); remote set is idempotent.
    r2 = sut_repo.git_ensure(paths, events, git_config=cfg, sut_root="sut")
    assert r2.ok is True
    assert not any(o["op"] == "init" for o in r2.ops)


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary required")
def test_no_auto_init_on_fresh_repo_returns_ok_with_zero_ops(tmp_path: Path) -> None:
    paths, events = _setup(tmp_path)
    r = sut_repo.git_ensure(
        paths,
        events,
        git_config={"enabled": True, "auto_init": False},
        sut_root="sut",
    )
    assert r.ok is True
    assert r.ops == []
    assert "auto_init" in r.summary or "nothing to do" in r.summary
