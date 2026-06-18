"""Issue #289 — per-project RAG memory (FTS5 index, build, query, isolation)."""
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


# ---------------------------------------------------------------------------
# Migration / schema
# ---------------------------------------------------------------------------


def test_fresh_db_has_memory_index(tmp_path):
    """A fresh init_db (schema.sql path) carries the memory_index FTS5 table."""
    conn, _ = _conn(tmp_path)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_index';"
    ).fetchone()
    assert row is not None


def test_migration_v14_to_v15(tmp_path):
    """An existing v14 DB gains the memory_index virtual table via migration."""
    from agentic_os.storage import db as db_mod

    db_path = tmp_path / "legacy.db"
    conn = db_mod.connect(db_path)
    conn.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);"
        "INSERT INTO schema_migrations(version, name, applied_at) "
        "VALUES (14, 'projects', '2026-01-01T00:00:00Z');"
    )
    assert db_mod.current_version(conn) == 14
    db_mod.migrate(conn)
    assert db_mod.current_version(conn) == db_mod.SCHEMA_VERSION
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
    }
    assert "memory_index" in names
    conn.close()


# ---------------------------------------------------------------------------
# Seed helpers — exercise the real per-source scoping chains.
# ---------------------------------------------------------------------------

SHARED_TERM = "kangaroo"


def _seed_project(conn, paths, *, project_id: str, suffix: str) -> dict:
    """Seed one project with all five sources carrying the shared term.

    Returns the source_ids written per source so the isolation test can assert
    exactly which rows belong to which project.
    """
    import json

    from agentic_os import projects

    projects.register_project(conn, name=project_id, sut_root=f"sites/{project_id}")

    ids: dict = {}

    # work_item (project-scoped) — the anchor transcripts/bugs link back to.
    wid = f"WI-{suffix}"
    conn.execute(
        "INSERT INTO work_items(id, title, status, spec_path, sut_root, priority, "
        "created_at, updated_at, project_id) VALUES (?,?,?,?,?,?,?,?,?);",
        (wid, f"{SHARED_TERM} feature {suffix}", "queued", "s.md", ".", "P2",
         "2026-05-01T00:00:00.000Z", "2026-05-01T00:00:00.000Z", project_id),
    )

    # learning (project-scoped directly).
    conn.execute(
        "INSERT INTO learnings(kind, subject, payload, observed_at, weight, actor, project_id) "
        "VALUES ('flaky', ?, '{}', '2026-05-02T00:00:00.000Z', 1.0, 'triager', ?);",
        (f"{SHARED_TERM}.feature::scn-{suffix}", project_id),
    )
    learning_id = conn.execute(
        "SELECT id FROM learnings WHERE project_id=?;", (project_id,)
    ).fetchone()[0]
    ids["learning"] = str(learning_id)

    # autonomy_session (project-scoped) + its summary markdown file on disk.
    sid = f"S-{suffix}"
    conn.execute(
        "INSERT INTO autonomy_sessions(id, started_at, finished_at, status, mode, project_id) "
        "VALUES (?, '2026-05-03T00:00:00.000Z', '2026-05-03T01:00:00.000Z', 'done', 'single', ?);",
        (sid, project_id),
    )
    reports_dir = paths.repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"session-summary-{sid}.md").write_text(
        f"# Session summary — {sid}\n\nThe {SHARED_TERM} hopped through {suffix}.\n",
        encoding="utf-8",
    )
    ids["summary"] = sid

    # transcript chain: task.payload.work_item_id -> work_item.project_id.
    phase_id = "P-MEM"
    conn.execute(
        "INSERT OR IGNORE INTO phases(id, status, branch, spec_path, updated_at) "
        "VALUES (?, 'in_progress', 'main', 's.md', '2026-05-01T00:00:00.000Z');",
        (phase_id,),
    )
    task_id = f"T-{suffix}"
    conn.execute(
        "INSERT INTO tasks(id, phase_id, kind, status, payload, created_at, updated_at) "
        "VALUES (?, ?, 'run', 'succeeded', ?, '2026-05-04T00:00:00.000Z', '2026-05-04T00:00:00.000Z');",
        (task_id, phase_id, json.dumps({"workflow": "run-tests", "work_item_id": wid})),
    )
    inv_id = f"INV-{suffix}"
    conn.execute(
        "INSERT INTO model_invocations(id, task_id, model_role, provider, command, started_at) "
        "VALUES (?, ?, 'opus', 'claude', '[\"x\"]', '2026-05-04T00:00:00.000Z');",
        (inv_id, task_id),
    )
    conn.execute(
        "INSERT INTO model_transcripts(invocation_id, kind, ord, payload, ts) "
        "VALUES (?, 'reasoning', 0, ?, '2026-05-04T00:00:00.000Z');",
        (inv_id, f"Considered the {SHARED_TERM} approach for {suffix}."),
    )
    ids["transcript"] = inv_id

    # bug file with a work_item_id back-ref in front-matter.
    bugs_dir = paths.repo_root / "bugs"
    bugs_dir.mkdir(parents=True, exist_ok=True)
    bug_id = f"BUG-{suffix}"
    (bugs_dir / f"{bug_id}.md").write_text(
        f"---\nwork_item_id: {wid}\nscenario: {SHARED_TERM}\n---\n"
        f"# {bug_id}\n\nThe {SHARED_TERM} crashes on {suffix}.\n",
        encoding="utf-8",
    )
    ids["bug"] = bug_id

    return ids


# ---------------------------------------------------------------------------
# THE discriminator — cross-project isolation per source.
# ---------------------------------------------------------------------------


def test_build_and_query_no_project_mixing(tmp_path):
    from agentic_os import memory

    conn, paths = _conn(tmp_path)
    a = _seed_project(conn, paths, project_id="alpha", suffix="A")
    b = _seed_project(conn, paths, project_id="beta", suffix="B")

    counts_a = memory.build_memory(conn, paths, project_id="alpha")
    counts_b = memory.build_memory(conn, paths, project_id="beta")

    # Each project indexed its own learning/summary/transcript/bug.
    assert counts_a["learning"] >= 1
    assert counts_a["summary"] >= 1
    assert counts_a["transcript"] >= 1
    assert counts_a["bug"] >= 1

    res_a = memory.query_memory(conn, project_id="alpha", text=SHARED_TERM, limit=20)
    res_b = memory.query_memory(conn, project_id="beta", text=SHARED_TERM, limit=20)

    by_source_a = {}
    for r in res_a:
        by_source_a.setdefault(r["source"], set()).add(r["source_id"])
    by_source_b = {}
    for r in res_b:
        by_source_b.setdefault(r["source"], set()).add(r["source_id"])

    # Per source: A sees only A's ids, B sees only B's — no leakage either way.
    for source in ("learning", "summary", "transcript", "bug"):
        assert a[source] in by_source_a.get(source, set()), source
        assert b[source] not in by_source_a.get(source, set()), source
        assert b[source] in by_source_b.get(source, set()), source
        assert a[source] not in by_source_b.get(source, set()), source


def test_build_memory_is_idempotent(tmp_path):
    from agentic_os import memory

    conn, paths = _conn(tmp_path)
    _seed_project(conn, paths, project_id="alpha", suffix="A")

    memory.build_memory(conn, paths, project_id="alpha")
    rows_first = conn.execute(
        "SELECT source, source_id, title, body FROM memory_index WHERE project_id='alpha' "
        "ORDER BY source, source_id;"
    ).fetchall()
    memory.build_memory(conn, paths, project_id="alpha")
    rows_second = conn.execute(
        "SELECT source, source_id, title, body FROM memory_index WHERE project_id='alpha' "
        "ORDER BY source, source_id;"
    ).fetchall()
    assert [tuple(r) for r in rows_first] == [tuple(r) for r in rows_second]
    assert len(rows_first) > 0


def test_query_ranks_by_relevance(tmp_path):
    from agentic_os import memory

    conn, paths = _conn(tmp_path)
    from agentic_os import projects

    projects.register_project(conn, name="alpha", sut_root=".")
    conn.execute(
        "INSERT INTO autonomy_sessions(id, started_at, status, mode, project_id) "
        "VALUES ('S1', '2026-05-03T00:00:00.000Z', 'done', 'single', 'alpha');"
    )
    conn.execute(
        "INSERT INTO autonomy_sessions(id, started_at, status, mode, project_id) "
        "VALUES ('S2', '2026-05-03T00:00:00.000Z', 'done', 'single', 'alpha');"
    )
    reports = paths.repo_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "session-summary-S1.md").write_text(
        "# S1\n\nzebra zebra zebra herd grazing zebra.\n", encoding="utf-8"
    )
    (reports / "session-summary-S2.md").write_text(
        "# S2\n\nA single zebra mention among elephants.\n", encoding="utf-8"
    )
    memory.build_memory(conn, paths, project_id="alpha")
    res = memory.query_memory(conn, project_id="alpha", text="zebra", limit=5)
    assert [r["source_id"] for r in res][:2] == ["S1", "S2"]


def test_hostile_match_query_does_not_raise(tmp_path):
    from agentic_os import memory

    conn, paths = _conn(tmp_path)
    _seed_project(conn, paths, project_id="alpha", suffix="A")
    memory.build_memory(conn, paths, project_id="alpha")
    # FTS5-hostile strings must never raise; they return [] or best-effort hits.
    for hostile in ('a AND ("', 'NEAR(', '"""', 'foo OR', '* * *', ')('):
        out = memory.query_memory(conn, project_id="alpha", text=hostile, limit=5)
        assert isinstance(out, list)
    # A normal multi-word query still finds the seeded content.
    hits = memory.query_memory(
        conn, project_id="alpha", text=f"{SHARED_TERM} feature", limit=5
    )
    assert len(hits) >= 1


# ---------------------------------------------------------------------------
# CLI smoke — `memory build` / `memory query <text>` scoped to active project.
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    cfg = repo / "config" / "agentic-os.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "sut:\n  root: .\nmodels: {}\ndashboard:\n  enable_write_endpoints: false\n",
        encoding="utf-8",
    )
    return repo


def test_cli_build_and_query(cli_repo: Path, capsys) -> None:
    import json as _json

    from agentic_os.cli import cmd_memory, open_runtime

    # Seed the default (active) project with a learning carrying the term.
    conn, _paths, _events, _orch = open_runtime(cli_repo)
    conn.execute(
        "INSERT INTO learnings(kind, subject, payload, observed_at, weight, actor, project_id) "
        "VALUES ('flaky', 'narwhal.feature::scn', '{}', '2026-05-02T00:00:00.000Z', 1.0, 'triager', 'default');"
    )
    conn.close()

    rc = cmd_memory(cli_repo, ["build"], json_output=True)
    out = capsys.readouterr().out
    assert rc == 0
    counts = _json.loads(out)["counts"]
    assert counts["learning"] >= 1

    rc = cmd_memory(cli_repo, ["query", "narwhal"], json_output=True)
    out = capsys.readouterr().out
    assert rc == 0
    results = _json.loads(out)["results"]
    assert any(r["source"] == "learning" for r in results)
