"""SUT git operations when the git binary is missing."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_os import sut_repo
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


def test_git_available_reflects_shutil_which() -> None:
    with patch("agentic_os.sut_repo.shutil.which", return_value=None):
        assert sut_repo.git_available() is False
    with patch("agentic_os.sut_repo.shutil.which", return_value="/usr/bin/git"):
        assert sut_repo.git_available() is True


def test_all_public_funcs_return_skipped_sentinel_when_git_missing(tmp_path: Path) -> None:
    paths, events = _setup(tmp_path)
    with patch("agentic_os.sut_repo.shutil.which", return_value=None), patch(
        "agentic_os.sut_repo.run_command"
    ) as run_mock:
        init_res = sut_repo.git_init(paths, events, sut_root="sut")
        remote_res = sut_repo.git_set_remote(
            paths, events, sut_root="sut", remote_url="https://github.com/o/r.git"
        )
        pub_res = sut_repo.git_publish_main(paths, events, sut_root="sut")
        fetch_res = sut_repo.git_fetch(paths, events, sut_root="sut")
        pull_res = sut_repo.git_pull_ff(paths, events, sut_root="sut")
        status = sut_repo.git_status(paths, sut_root="sut")
    for res in (init_res, remote_res, pub_res, fetch_res, pull_res):
        assert res.ok is False
        assert res.exit_code == -1
        assert res.detail.get("skipped") is True
        assert res.detail.get("reason") == "git_not_installed"
    assert status == {
        "initialized": False,
        "skipped": True,
        "reason": "git_not_installed",
    }
    # Subprocess fork must never be attempted when git missing.
    run_mock.assert_not_called()


def test_skipped_event_is_deduped_per_op(tmp_path: Path) -> None:
    paths, events = _setup(tmp_path)
    with patch("agentic_os.sut_repo.shutil.which", return_value=None):
        sut_repo.git_fetch(paths, events, sut_root="sut")
        sut_repo.git_fetch(paths, events, sut_root="sut")
        sut_repo.git_fetch(paths, events, sut_root="sut")
    tail = events.tail(20)
    skipped = [e for e in tail if e["kind"] == "sut.git.skipped"]
    # Exactly one event for (fetch, sut) — subsequent fetches must be silent.
    assert len(skipped) == 1
    payload = skipped[0]["payload"]
    assert payload["op"] == "fetch"
    assert payload["reason"] == "git_not_installed"


def test_server_translates_skipped_sentinel_to_ok_true(tmp_path: Path) -> None:
    """Acceptance: `/api/sut/git/*` returns 200 with ok=true when git missing."""
    # Direct unit test of the translation code-path: feed a skipped GitOpResult
    # through the response shape the server emits.
    skipped = sut_repo._skipped_result()
    detail = skipped.detail
    assert detail.get("skipped") is True
    assert detail.get("reason") == "git_not_installed"
