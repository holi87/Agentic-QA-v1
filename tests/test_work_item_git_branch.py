"""Per-work-item branch creation, SUT autocommit, and diff behavior."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentic_os import sut_repo
from agentic_os.events import EventLog
from agentic_os.paths import RuntimePaths
from agentic_os.runtime.subprocess import run_command
from agentic_os.storage import init_db

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary required")


def _init_sut_repo(target: Path, paths: RuntimePaths) -> None:
    log = paths.subprocess_logs_dir / "setup.log"
    (target / "README.md").write_text("hi", encoding="utf-8")
    run_command(["git", "init"], cwd=target, log_path=log, timeout_seconds=15)
    run_command(["git", "add", "-A"], cwd=target, log_path=log, timeout_seconds=15)
    run_command(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
        cwd=target,
        log_path=log,
        timeout_seconds=15,
    )
    # Normalize default branch to main for deterministic base.
    run_command(["git", "branch", "-M", "main"], cwd=target, log_path=log, timeout_seconds=10)


def _setup(tmp_path: Path) -> tuple[RuntimePaths, EventLog, Path]:
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    sut = paths.repo_root / "sut"
    sut.mkdir(parents=True, exist_ok=True)
    _init_sut_repo(sut, paths)
    conn = init_db(paths.db)
    sut_repo._SKIPPED_NOTIFIED.clear()
    return paths, EventLog(conn, paths), sut


def test_branch_name_is_canonical() -> None:
    name = sut_repo.work_item_branch_name("WI-42", "Add login coverage!!")
    assert name == "agentic-os/wi-WI-42-add-login-coverage"


def test_start_branch_then_autocommit_three_files(tmp_path: Path) -> None:
    paths, events, sut = _setup(tmp_path)
    res = sut_repo.git_start_work_item_branch(
        paths, events, sut_root="sut", work_item_id="WI-1", title="cover", base="main"
    )
    assert res.ok is True
    assert res.detail["branch"] == "agentic-os/wi-WI-1-cover"

    files = []
    for i in range(3):
        rel = f"tests/generated/spec_{i}.py"
        (sut / "tests" / "generated").mkdir(parents=True, exist_ok=True)
        (sut / rel).write_text(f"# spec {i}\n", encoding="utf-8")
        files.append(rel)

    commits = sut_repo.git_autocommit(
        paths, events, sut_root="sut", work_item_id="WI-1", files=files, title="cover"
    )
    assert len(commits) == 3
    assert all(c.ok for c in commits)

    # 3 commits on the branch above base.
    log = paths.subprocess_logs_dir / "count.log"
    run_command(
        ["git", "rev-list", "--count", "main..HEAD"],
        cwd=sut,
        log_path=log,
        timeout_seconds=10,
    )
    text = log.read_text(encoding="utf-8")
    count = next(
        (line[len("[stdout] "):].strip() for line in text.splitlines() if line.startswith("[stdout] ")),
        "0",
    )
    assert count == "3"


def test_autocommit_idempotent_on_unchanged_file(tmp_path: Path) -> None:
    paths, events, sut = _setup(tmp_path)
    sut_repo.git_start_work_item_branch(
        paths, events, sut_root="sut", work_item_id="WI-2", title="x", base="main"
    )
    (sut / "tests").mkdir(parents=True, exist_ok=True)
    (sut / "tests/a.py").write_text("a\n", encoding="utf-8")
    first = sut_repo.git_autocommit(
        paths, events, sut_root="sut", work_item_id="WI-2", files=["tests/a.py"], title="x"
    )
    assert first[0].detail.get("committed") is True
    second = sut_repo.git_autocommit(
        paths, events, sut_root="sut", work_item_id="WI-2", files=["tests/a.py"], title="x"
    )
    assert second[0].detail.get("skipped") is True


def test_dirty_base_refuses_branch_create(tmp_path: Path) -> None:
    paths, events, sut = _setup(tmp_path)
    # Leave the working tree dirty on main.
    (sut / "dirty.txt").write_text("uncommitted", encoding="utf-8")
    res = sut_repo.git_start_work_item_branch(
        paths, events, sut_root="sut", work_item_id="WI-3", title="x", base="main"
    )
    assert res.ok is False
    assert res.detail["reason"] == "dirty_working_tree"


def test_work_item_diff_returns_unified(tmp_path: Path) -> None:
    paths, events, sut = _setup(tmp_path)
    sut_repo.git_start_work_item_branch(
        paths, events, sut_root="sut", work_item_id="WI-4", title="d", base="main"
    )
    (sut / "tests").mkdir(parents=True, exist_ok=True)
    (sut / "tests/new.py").write_text("print(1)\n", encoding="utf-8")
    sut_repo.git_autocommit(
        paths, events, sut_root="sut", work_item_id="WI-4", files=["tests/new.py"], title="d"
    )
    out = sut_repo.git_work_item_diff(
        paths, sut_root="sut", work_item_id="WI-4", title="d", base="main"
    )
    assert out["ok"] is True
    assert "tests/new.py" in out["diff"]
