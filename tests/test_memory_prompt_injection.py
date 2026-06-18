"""Issue #289 — memory_context_block render/budget/role gating + injection."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_os.paths import RuntimePaths
from agentic_os.storage import init_db


def _conn(tmp_path: Path):
    paths = RuntimePaths(
        repo_root=tmp_path / "repo", runtime_root=tmp_path / "repo" / ".agentic-os"
    )
    paths.ensure()
    return init_db(paths.db), paths


def _seed_indexed(conn, paths, *, project_id="default", term="platypus"):
    """Index one learning carrying ``term`` for ``project_id`` into memory."""
    from agentic_os import memory

    conn.execute(
        "INSERT INTO learnings(kind, subject, payload, observed_at, weight, actor, project_id) "
        "VALUES ('flaky', ?, '{}', '2026-05-02T00:00:00.000Z', 1.0, 'triager', ?);",
        (f"{term}.feature::scn", project_id),
    )
    memory.build_memory(conn, paths, project_id=project_id)


def test_block_none_for_unsupported_role(tmp_path):
    from agentic_os import memory_context

    conn, paths = _conn(tmp_path)
    _seed_indexed(conn, paths)
    for role in ("reviewer", "triager", "orchestrator"):
        assert (
            memory_context.memory_context_block(
                conn, project_id="default", role=role, text="platypus",
                budget_tokens=500, top_k=5,
            )
            is None
        )


def test_block_none_when_empty(tmp_path):
    from agentic_os import memory_context

    conn, paths = _conn(tmp_path)
    # No indexed memory at all → nothing to inject.
    assert (
        memory_context.memory_context_block(
            conn, project_id="default", role="planner", text="anything",
            budget_tokens=500, top_k=5,
        )
        is None
    )


def test_block_renders_prior_context(tmp_path):
    from agentic_os import memory_context

    conn, paths = _conn(tmp_path)
    _seed_indexed(conn, paths, term="platypus")
    block = memory_context.memory_context_block(
        conn, project_id="default", role="planner", text="platypus",
        budget_tokens=500, top_k=5,
    )
    assert block is not None
    assert "## Prior context" in block
    assert "platypus" in block.lower()


def test_memory_block_respects_budget(tmp_path):
    from agentic_os import memory_context
    from agentic_os.budgets import estimate_tokens

    conn, paths = _conn(tmp_path)
    # Seed many large summaries so the unbounded render would blow the budget.
    from agentic_os import memory

    for i in range(10):
        sid = f"S{i}"
        conn.execute(
            "INSERT INTO autonomy_sessions(id, started_at, status, mode, project_id) "
            "VALUES (?, '2026-05-03T00:00:00.000Z', 'done', 'single', 'default');",
            (sid,),
        )
        reports = paths.repo_root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"session-summary-{sid}.md").write_text(
            "# " + sid + "\n\n" + ("walrus " * 400) + "\n", encoding="utf-8"
        )
    memory.build_memory(conn, paths, project_id="default")
    budget = 60
    block = memory_context.memory_context_block(
        conn, project_id="default", role="implementer", text="walrus",
        budget_tokens=budget, top_k=5,
    )
    assert block is not None
    assert estimate_tokens(block) <= budget


def test_isolation_block_only_active_project(tmp_path):
    from agentic_os import memory, memory_context
    from agentic_os import projects

    conn, paths = _conn(tmp_path)
    projects.register_project(conn, name="alpha", sut_root=".")
    # Term lives only in alpha; default has nothing.
    conn.execute(
        "INSERT INTO learnings(kind, subject, payload, observed_at, weight, actor, project_id) "
        "VALUES ('flaky', 'quokka.feature::scn', '{}', '2026-05-02T00:00:00.000Z', 1.0, 'triager', 'alpha');"
    )
    memory.build_memory(conn, paths, project_id="alpha")
    memory.build_memory(conn, paths, project_id="default")
    # Querying default for alpha's term yields nothing.
    assert (
        memory_context.memory_context_block(
            conn, project_id="default", role="planner", text="quokka",
            budget_tokens=500, top_k=5,
        )
        is None
    )
    # Querying alpha finds it.
    block = memory_context.memory_context_block(
        conn, project_id="alpha", role="planner", text="quokka",
        budget_tokens=500, top_k=5,
    )
    assert block is not None and "quokka" in block.lower()


# ---------------------------------------------------------------------------
# Config — memory_* keys under prompt_context.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_memory_config_validates_and_rejects_bad_types(tmp_path):
    from agentic_os.config import load_config
    from agentic_os.errors import ConfigError

    base = (REPO_ROOT / "config" / "agentic-os.yml.example").read_text(encoding="utf-8")
    good = tmp_path / "good.yml"
    good.write_text(base, encoding="utf-8")
    cfg = load_config(good)
    assert cfg.raw["prompt_context"]["memory_enabled"] is True
    assert cfg.raw["prompt_context"]["memory_budget_tokens"] == 500
    assert cfg.raw["prompt_context"]["memory_top_k"] == 5

    bad = tmp_path / "bad.yml"
    bad.write_text(
        base.replace("memory_budget_tokens: 500", 'memory_budget_tokens: "many"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(bad)


# ---------------------------------------------------------------------------
# Injection path through invoke_model (mirror test_learnings_prompt_injection).
# ---------------------------------------------------------------------------

import os  # noqa: E402

from agentic_os.events import EventLog  # noqa: E402
from agentic_os.models import invoke_model  # noqa: E402


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


def test_planner_invocation_injects_and_emits_memory_consulted(tmp_path):
    conn, paths, events = _runtime(tmp_path)
    try:
        _seed_indexed(conn, paths, term="hippopotamus")
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
                prompt="please plan the hippopotamus feature",
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(str(fake.parent) + os.pathsep, "")
        text = (paths.repo_root / result.input_path).read_text(encoding="utf-8")
        assert "Prior context" in text
        assert "hippopotamus" in text.lower()
        consulted = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='memory.consulted' AND actor='planner';"
        ).fetchone()[0]
        assert consulted >= 1
    finally:
        conn.close()


def test_reviewer_invocation_does_not_inject_memory(tmp_path):
    conn, paths, events = _runtime(tmp_path)
    try:
        _seed_indexed(conn, paths, term="hippopotamus")
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
                prompt="please review the hippopotamus feature",
                timeout_seconds=5,
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(str(fake.parent) + os.pathsep, "")
        text = (paths.repo_root / result.input_path).read_text(encoding="utf-8")
        assert "Prior context" not in text
        consulted = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='memory.consulted' AND actor='reviewer';"
        ).fetchone()[0]
        assert consulted == 0
    finally:
        conn.close()


def test_memory_injection_failure_never_breaks_invocation(tmp_path, monkeypatch):
    conn, paths, events = _runtime(tmp_path)
    try:
        _seed_indexed(conn, paths, term="hippopotamus")

        import agentic_os.memory_context as mc

        def _boom(*args, **kwargs):
            raise RuntimeError("synthetic memory failure")

        monkeypatch.setattr(mc, "memory_context_block", _boom)

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
                prompt="please plan the hippopotamus feature",
            )
        finally:
            os.environ["PATH"] = os.environ["PATH"].replace(str(fake.parent) + os.pathsep, "")
        assert result.exit_code == 0
        failed = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='memory.injection_failed';"
        ).fetchone()[0]
        assert failed >= 1
    finally:
        conn.close()
