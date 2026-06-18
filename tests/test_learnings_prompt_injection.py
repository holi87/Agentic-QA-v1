"""Issue #287 — learnings prompt injection (learnings_context + invoke path)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_os import learnings
from agentic_os.budgets import estimate_tokens
from agentic_os.events import EventLog
from agentic_os.learnings_context import (
    DEFAULT_BUDGET_TOKENS,
    learnings_context_block,
)
from agentic_os.models import invoke_model
from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


def _runtime(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = RuntimePaths(repo_root=repo, runtime_root=repo / ".agentic-os")
    paths.ensure()
    conn = init_db(paths.db)
    events = EventLog(conn, paths)
    return conn, paths, events


def _write_script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o755)


def _seed_learnings(conn) -> None:
    learnings.record_learning(
        conn, kind="flaky", subject="feat.login::flaky-scn", payload={}, actor="triager"
    )
    learnings.record_learning(
        conn,
        kind="coverage_gap",
        subject="orders-api::security",
        payload={"missing": ["agentic-os:companion:neg-auth"]},
        actor="coverage-review",
    )
    learnings.record_learning(
        conn,
        kind="skill_failure",
        subject="reviewer::api",
        payload={"reason": "coverage_floor_missing", "consecutive": 2},
        actor="review-gate",
    )


# ---------------------------------------------------------------------------
# learnings_context_block unit behaviour
# ---------------------------------------------------------------------------


def test_block_contains_seeded_subjects(tmp_path: Path) -> None:
    conn, _paths, _events = _runtime(tmp_path)
    try:
        _seed_learnings(conn)
        block = learnings_context_block(conn, role="planner", budget_tokens=DEFAULT_BUDGET_TOKENS)
        assert block is not None
        assert "feat.login::flaky-scn" in block
        assert "orders-api::security" in block
        assert "reviewer::api" in block
    finally:
        conn.close()


def test_block_none_without_learnings(tmp_path: Path) -> None:
    conn, _paths, _events = _runtime(tmp_path)
    try:
        block = learnings_context_block(conn, role="planner", budget_tokens=DEFAULT_BUDGET_TOKENS)
        assert block is None
    finally:
        conn.close()


def test_block_respects_budget(tmp_path: Path) -> None:
    conn, _paths, _events = _runtime(tmp_path)
    try:
        # Seed many flaky subjects so the unbounded block would overflow.
        for i in range(200):
            learnings.record_learning(
                conn, kind="flaky", subject=f"feat::scn-{i:03d}", payload={}, actor="triager"
            )
        block = learnings_context_block(conn, role="planner", budget_tokens=40)
        assert block is not None
        assert estimate_tokens(block) <= 40 + 30  # budget + wrapper headroom
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Injection path through invoke_model
# ---------------------------------------------------------------------------


def test_planner_invocation_injects_and_emits_consulted(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        _seed_learnings(conn)
        fake = tmp_path / "bin" / "fake-claude"
        _write_script(fake, "#!/usr/bin/env bash\nprintf 'ok\\n'\n")
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        try:
            result = invoke_model(
                conn,
                paths,
                events,
                role="planner",
                config={"planner": {"provider": "claude", "command": [str(fake)], "role": "opus"}},
                prompt="please plan",
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(str(fake.parent) + os.pathsep, "")
        text = (paths.repo_root / result.input_path).read_text(encoding="utf-8")
        assert "feat.login::flaky-scn" in text
        assert "please plan" in text
        consulted = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='learning.consulted' AND actor='planner';"
        ).fetchone()[0]
        assert consulted >= 1
    finally:
        conn.close()


def test_reviewer_invocation_does_not_inject_learnings(tmp_path: Path) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        _seed_learnings(conn)
        fake = tmp_path / "bin" / "fake-claude"
        _write_script(
            fake,
            "#!/usr/bin/env bash\nprintf 'verdict: APPROVE\\nreason: ok\\n\\nfindings:\\n- OK:1 - none\\nREADY\\n'\n",
        )
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        try:
            result = invoke_model(
                conn,
                paths,
                events,
                role="reviewer",
                config={"reviewer": {"provider": "codex", "command": [str(fake)], "role": "codex"}},
                prompt="please review",
                timeout_seconds=5,
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(str(fake.parent) + os.pathsep, "")
        text = (paths.repo_root / result.input_path).read_text(encoding="utf-8")
        assert "feat.login::flaky-scn" not in text
        consulted = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='learning.consulted' AND actor='reviewer';"
        ).fetchone()[0]
        assert consulted == 0
    finally:
        conn.close()


def test_injection_failure_never_breaks_invocation(tmp_path: Path, monkeypatch) -> None:
    conn, paths, events = _runtime(tmp_path)
    try:
        _seed_learnings(conn)

        import agentic_os.learnings_context as lc

        def _boom(*args, **kwargs):
            raise RuntimeError("synthetic learnings failure")

        monkeypatch.setattr(lc, "learnings_context_block", _boom)

        fake = tmp_path / "bin" / "fake-claude"
        _write_script(fake, "#!/usr/bin/env bash\nprintf 'ok\\n'\n")
        os.environ["PATH"] = str(fake.parent) + os.pathsep + os.environ["PATH"]
        try:
            result = invoke_model(
                conn,
                paths,
                events,
                role="planner",
                config={"planner": {"provider": "claude", "command": [str(fake)], "role": "opus"}},
                prompt="please plan",
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(str(fake.parent) + os.pathsep, "")
        # Invocation still completed despite the injection blowing up.
        assert result.exit_code == 0
        failed = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='learning.injection_failed';"
        ).fetchone()[0]
        assert failed >= 1
    finally:
        conn.close()
