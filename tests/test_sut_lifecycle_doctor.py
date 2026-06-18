"""SUT lifecycle, Docker Compose command construction, healthchecks, and doctor checks."""
from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest import mock

import pytest

from agentic_os.errors import UsageError
from agentic_os.events import EventLog
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db
from agentic_os.sut_lifecycle import (
    INFRA_EXIT_CODE,
    build_compose_argv,
    doctor_check_docker,
    doctor_check_models,
    doctor_check_sut,
    run_sut_healthcheck,
    run_sut_start,
    run_sut_stop,
)


def _runtime(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    return conn, paths, events


def test_build_compose_argv_up_detached() -> None:
    argv = build_compose_argv(
        compose_file="docker-compose.yml",
        compose_project_name="proj",
        action="up",
    )
    assert argv == ["docker", "compose", "-f", "docker-compose.yml", "-p", "proj", "up", "-d"]


def test_build_compose_argv_down_volumes_optin() -> None:
    no_vols = build_compose_argv(
        compose_file="docker-compose.yml",
        compose_project_name="proj",
        action="down",
    )
    with_vols = build_compose_argv(
        compose_file="docker-compose.yml",
        compose_project_name="proj",
        action="down",
        volumes=True,
    )
    assert "--volumes" not in no_vols
    assert with_vols[-1] == "--volumes"


def test_build_compose_argv_rejects_unknown_action() -> None:
    with pytest.raises(UsageError):
        build_compose_argv(
            compose_file="docker-compose.yml",
            compose_project_name="proj",
            action="exec",
        )


def test_sut_start_skips_without_compose_file(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        res = run_sut_start(paths, events, compose_file=None, compose_project_name=None)
        assert res.ok is True
        assert res.exit_code == 0
        assert res.detail["skipped"] is True
    finally:
        conn.close()


def test_sut_start_infra_fail_when_docker_missing(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        (paths.repo_root / "docker-compose.yml").write_text("version: '3'\n", encoding="utf-8")
        with mock.patch("agentic_os.sut_lifecycle.shutil.which", return_value=None):
            res = run_sut_start(
                paths,
                events,
                compose_file="docker-compose.yml",
                compose_project_name="proj",
            )
        assert res.ok is False
        assert res.exit_code == INFRA_EXIT_CODE
        assert res.failure_kind == "infra_missing_docker"
    finally:
        conn.close()


def test_sut_start_infra_fail_when_compose_file_missing(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        # docker present (we mock it), compose file does NOT exist.
        with mock.patch("agentic_os.sut_lifecycle.shutil.which", return_value="/usr/bin/docker"):
            res = run_sut_start(
                paths,
                events,
                compose_file="missing-compose.yml",
                compose_project_name="proj",
            )
        assert res.exit_code == INFRA_EXIT_CODE
        assert res.failure_kind == "infra_missing_compose_file"
    finally:
        conn.close()


def test_healthcheck_passes_first_attempt(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        res = run_sut_healthcheck(
            paths,
            events,
            command=["true"],
            timeout_seconds=5,
            retries=2,
        )
        assert res.ok is True
        assert res.exit_code == 0
        assert res.detail["attempts"][0]["attempt"] == 1
    finally:
        conn.close()


def test_healthcheck_infra_fail_after_retries(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        res = run_sut_healthcheck(
            paths,
            events,
            command=["false"],
            timeout_seconds=2,
            retries=1,
        )
        assert res.ok is False
        assert res.exit_code == INFRA_EXIT_CODE
        assert res.failure_kind == "infra_healthcheck_timeout"
        assert len(res.detail["attempts"]) == 2
    finally:
        conn.close()


def test_healthcheck_argv_required(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        with pytest.raises(UsageError):
            run_sut_healthcheck(
                paths,
                events,
                command=[],
                timeout_seconds=1,
                retries=0,
            )
    finally:
        conn.close()


def test_sut_stop_skips_without_compose(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        res = run_sut_stop(paths, events, compose_file=None, compose_project_name=None)
        assert res.ok is True
        assert res.detail["skipped"] is True
    finally:
        conn.close()


def test_doctor_check_docker_reports_status() -> None:
    info = doctor_check_docker()
    # Either docker is present (info["docker"] is a str) or not (None).
    assert "docker" in info
    assert "compose" in info


def test_doctor_check_sut_flags_missing_compose(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        info = doctor_check_sut(
            paths,
            {
                "compose_file": "no-such.yml",
                "test_runner": "./missing-runner.sh",
                "healthcheck": {"command": ["true"]},
            },
        )
        joined = " ".join(info["issues"])
        assert "compose_file missing" in joined
        assert "test_runner missing" in joined
    finally:
        conn.close()


def test_doctor_check_sut_online_mode_ignores_missing_compose(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        (paths.repo_root / "run-tests.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        os.chmod(paths.repo_root / "run-tests.sh", 0o755)
        info = doctor_check_sut(
            paths,
            {
                "mode": "online",
                "compose_file": None,
                "test_runner": "./run-tests.sh",
                "healthcheck": {"command": ["true"]},
                "web": {"enabled": True, "url": "http://127.0.0.1:3000"},
                "api": {"enabled": True, "url": "http://127.0.0.1:3000/api"},
            },
        )
        joined = " ".join(info["issues"])
        assert "compose_file missing" not in joined
        assert info["mode"] == "online"
        assert info["warnings"], "online mode should still surface missing optional discovery sources"
    finally:
        conn.close()


def test_doctor_check_sut_rejects_shell_string() -> None:
    info = doctor_check_sut(
        RuntimePaths(repo_root=Path("/tmp/never"), runtime_root=Path("/tmp/never/.agentic-os")),
        {
            "compose_file": None,
            "test_runner": None,
            "healthcheck": {"command": "true && false"},  # not argv list
        },
    )
    joined = " ".join(info["issues"])
    assert "argv list" in joined


def test_doctor_check_models_reports_missing_binary() -> None:
    import sys as _sys

    # Use the actual python interpreter executable as the "present" example.
    real_bin = _sys.executable
    info = doctor_check_models(
        {
            "planner": {"command": ["definitely-not-a-real-binary-xyz", "--help"]},
            "implementer": {"command": []},
            "reviewer": {"command": [real_bin]},
            "triager": {"command": [real_bin, "-c", "pass"]},
        }
    )
    assert "planner binary not on PATH" in " ".join(info["issues"])
    assert "implementer.command missing" in " ".join(info["issues"])
    assert info["reviewer"]["found"] is True
    assert info["triager"]["found"] is True
