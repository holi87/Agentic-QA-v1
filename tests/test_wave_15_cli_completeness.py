"""Wave 15 (#315) — CLI completeness + truthful config readiness.

Covers epic acceptance:
* ``up`` runs without ``--dashboard-only`` (real orchestrator daemon path
  starts an autonomy session alongside the dashboard).
* ``--dashboard-only`` keeps a true read-only console — refuses
  ``--autonomy-minutes``.
* ``init --sample-sut`` copies the shipped scaffold and rewrites
  ``config/agentic-os.yml`` so a fresh checkout can ``agentic-os up``
  end-to-end.
* The sample SUT template files actually exist in the repo so the CLI
  copy step is never trying to copy from a missing source.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import List

import pytest

from agentic_os.cli import _install_sample_sut, cmd_up
from agentic_os.errors import UsageError
from agentic_os.events import EventLog
from agentic_os.orchestrator import Orchestrator
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Sample SUT scaffold
# ---------------------------------------------------------------------------


def test_sample_sut_template_exists_in_repo() -> None:
    template = (
        REPO_ROOT
        / "scripts"
        / "agentic-os"
        / "templates"
        / "sample-sut"
    )
    assert template.is_dir(), template
    # Minimum payload the operator gets after init --sample-sut.
    for rel in ("docker-compose.yml", "openapi.yaml", "README.md", "public/index.html"):
        assert (template / rel).is_file(), rel


def test_install_sample_sut_copies_files_and_patches_config(tmp_path: Path) -> None:
    # Minimal config so the YAML patch path runs end-to-end.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "agentic-os.yml").write_text(
        "runtime:\n  root: .agentic-os\n", encoding="utf-8"
    )

    info = _install_sample_sut(tmp_path, force=False)

    assert info["target_dir"] == "sample-sut"
    assert "docker-compose.yml" in info["files_copied"]
    assert "openapi.yaml" in info["files_copied"]
    assert any("public/index.html" in f for f in info["files_copied"])
    sample_dir = tmp_path / "sample-sut"
    assert (sample_dir / "docker-compose.yml").is_file()
    assert (sample_dir / "openapi.yaml").is_file()

    # Config rewritten to point at the new scaffold.
    assert info["config_updated"] is True
    import yaml  # type: ignore

    cfg = yaml.safe_load((cfg_dir / "agentic-os.yml").read_text(encoding="utf-8"))
    assert cfg["sut"]["compose_file"] == "sample-sut/docker-compose.yml"
    assert cfg["sut"]["web"]["url"] == "http://localhost:8080"
    assert "sample-sut/openapi.yaml" in cfg["sut"]["api"]["openapi"]["sources"]


def test_install_sample_sut_refuses_overwrite_without_force(tmp_path: Path) -> None:
    (tmp_path / "sample-sut").mkdir()
    with pytest.raises(UsageError, match=r"sample-sut/ already exists"):
        _install_sample_sut(tmp_path, force=False)


def test_install_sample_sut_force_refreshes_template_files_only(tmp_path: Path) -> None:
    """--force is *additive*: every template file is re-copied, but
    operator additions outside the template (e.g. a hand-written
    ``notes.md``) are preserved so a re-init does not wipe local
    customizations. The behavior is deliberate; the test pins it so a
    future change to nuke-and-pave force is loud."""
    sample_dir = tmp_path / "sample-sut"
    sample_dir.mkdir()
    # 1) Operator-added file outside the template — must survive.
    (sample_dir / "notes.md").write_text("local notes", encoding="utf-8")
    # 2) A template file the operator edited — must be reset by --force.
    (sample_dir / "docker-compose.yml").write_text("stale", encoding="utf-8")

    info = _install_sample_sut(tmp_path, force=True)

    assert info["target_dir"] == "sample-sut"
    assert (sample_dir / "notes.md").read_text(encoding="utf-8") == "local notes"
    refreshed = (sample_dir / "docker-compose.yml").read_text(encoding="utf-8")
    assert refreshed != "stale"
    assert "services:" in refreshed  # actual compose content landed


# ---------------------------------------------------------------------------
# up — orchestrator daemon argument plumbing
# ---------------------------------------------------------------------------


def test_up_without_dashboard_only_starts_autonomy_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive path — `agentic-os up` (no `--dashboard-only`) calls
    `autonomy.start_session` before `serve_blocking`. Verified via
    monkeypatch so the test doesn't actually need a live server."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "agentic-os.yml").write_text(
        "runtime:\n  root: .agentic-os\n", encoding="utf-8"
    )

    started: list[tuple[Path, int]] = []

    class _FakeSession:
        session_id = "test-session"

    def _fake_start_session(paths, max_minutes):
        started.append((paths.runtime_root, max_minutes))
        return _FakeSession()

    def _fake_serve_blocking(paths, *, host, port):
        return 0

    from agentic_os import autonomy as _autonomy
    from agentic_os import server as _server

    monkeypatch.setattr(_autonomy, "start_session", _fake_start_session)
    monkeypatch.setattr(_server, "serve_blocking", _fake_serve_blocking)
    # cmd_up imports serve_blocking inline — patch the symbol it looks up.
    from agentic_os import cli as _cli

    monkeypatch.setattr(_cli, "open_runtime", _cli.open_runtime)  # touch for ordering

    rc = cmd_up(tmp_path, ["--autonomy-minutes", "5"], json_output=False)
    assert rc == 0
    assert len(started) == 1
    runtime_root, minutes = started[0]
    assert minutes == 5
    # Runtime root resolved via config defaults — assert it sits under
    # the test repo (the exact directory name comes from the default
    # config, which is fine to evolve).
    assert tmp_path in runtime_root.parents or runtime_root == tmp_path / runtime_root.name


def test_up_dashboard_only_does_not_start_autonomy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "agentic-os.yml").write_text(
        "runtime:\n  root: .agentic-os\n", encoding="utf-8"
    )

    started: list[str] = []
    from agentic_os import autonomy as _autonomy
    from agentic_os import server as _server

    monkeypatch.setattr(
        _autonomy,
        "start_session",
        lambda paths, max_minutes: started.append("called"),  # type: ignore[misc]
    )
    monkeypatch.setattr(_server, "serve_blocking", lambda paths, *, host, port: 0)

    rc = cmd_up(tmp_path, ["--dashboard-only"], json_output=False)
    assert rc == 0
    assert started == [], "dashboard-only must not start an autonomy session"


def test_up_dashboard_only_refuses_autonomy_minutes(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "agentic-os.yml").write_text(
        "runtime:\n  root: .agentic-os\n", encoding="utf-8"
    )
    with pytest.raises(UsageError, match=r"incompatible with --dashboard-only"):
        cmd_up(
            tmp_path,
            ["--dashboard-only", "--autonomy-minutes", "30"],
            json_output=False,
        )


def test_up_help_advertises_orchestrator_daemon_mode() -> None:
    """The CLI top-level help banner must not still claim only
    ``--dashboard-only`` is supported — Wave 15 wires the daemon path."""
    out = subprocess.run(
        [sys.executable, "-m", "agentic_os", "--help"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT / "scripts" / "agentic-os"),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert out.returncode == 0, out.stderr
    assert "orchestrator daemon" in out.stdout, out.stdout
    # The misleading legacy line must be gone.
    assert "--dashboard-only is the supported mode" not in out.stdout


def test_up_subcommand_advertises_autonomy_minutes() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "agentic_os", "up", "--help"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT / "scripts" / "agentic-os"),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert out.returncode == 0, out.stderr
    assert "--autonomy-minutes" in out.stdout
