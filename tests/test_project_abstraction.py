"""Issue #288 — addressable projects over the flat work_items list.

Migration v14 introduces a `projects` table, seeds a literal `default`
project, and backfills `project_id` onto `work_items`, `autonomy_sessions`
and `learnings`. These tests pin both schema paths the runtime can take:

- fresh install (db.py runs `schema.sql` verbatim and stamps SCHEMA_VERSION);
- in-place upgrade of an existing v13 runtime (the incremental migration).

Both must reach the same shape and pass `assert_db_healthy` (FK + integrity).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_os.errors import UsageError
from agentic_os.projects import (
    DEFAULT_PROJECT_ID,
    ensure_default_project,
    get_project,
    list_projects,
    register_project,
    resolve_active_project_id,
)
from agentic_os.events import EventLog
from agentic_os.paths import runtime_paths
from agentic_os.storage.db import (
    SCHEMA_VERSION,
    assert_db_healthy,
    connect,
    current_version,
    init_db,
    migrate,
)
from agentic_os.work_items import (
    create_work_item_from_payload,
    get_work_item,
    list_work_items,
)


class _Cfg:
    """Minimal stand-in for AgenticConfig (only ``.raw`` is read)."""

    def __init__(self, raw: dict) -> None:
        self.raw = raw


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table});").fetchall()}


def test_fresh_install_seeds_default_project(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        assert current_version(conn) == SCHEMA_VERSION == 16
        rows = conn.execute(
            "SELECT id, name, sut_root FROM projects;"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("default", "default", ".")]
        assert "project_id" in _columns(conn, "work_items")
        assert "project_id" in _columns(conn, "autonomy_sessions")
        assert "project_id" in _columns(conn, "learnings")
        assert_db_healthy(conn)
    finally:
        conn.close()


def test_upgrade_from_v13_backfills_default_project(tmp_path: Path) -> None:
    """Seed a minimal v13 runtime with live rows, then migrate to v14."""
    conn = connect(tmp_path / "state.db")
    try:
        conn.executescript(
            """
            CREATE TABLE schema_migrations(
              version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
            CREATE TABLE work_items(
              id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
              spec_path TEXT NOT NULL, sut_root TEXT NOT NULL, priority TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE autonomy_sessions(
              id TEXT PRIMARY KEY, started_at TEXT NOT NULL, status TEXT NOT NULL,
              mode TEXT NOT NULL);
            CREATE TABLE learnings(
              id INTEGER PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
              payload TEXT NOT NULL, observed_at TEXT NOT NULL, weight REAL NOT NULL,
              actor TEXT NOT NULL);
            INSERT INTO work_items VALUES(
              'TASK-1','t','queued','s.md','.','P2',
              '2026-01-01T00:00:00.000Z','2026-01-01T00:00:00.000Z');
            INSERT INTO autonomy_sessions VALUES(
              'S1','2026-01-01T00:00:00.000Z','done','single');
            INSERT INTO learnings VALUES(
              1,'flaky','x','{}','2026-01-01T00:00:00.000Z',1.0,'script');
            INSERT INTO schema_migrations VALUES(
              13,'learnings','2026-01-01T00:00:00.000Z');
            """
        )
        assert current_version(conn) == 13

        migrate(conn)

        assert current_version(conn) == SCHEMA_VERSION
        # Every pre-existing row is backfilled onto the default project — no
        # NULL project_id survives, so single-SUT runtimes keep working.
        assert conn.execute("SELECT project_id FROM work_items;").fetchone()[0] == "default"
        assert conn.execute("SELECT project_id FROM autonomy_sessions;").fetchone()[0] == "default"
        assert conn.execute("SELECT project_id FROM learnings;").fetchone()[0] == "default"
        assert conn.execute(
            "SELECT name, sut_root FROM projects WHERE id='default';"
        ).fetchone()[0] == "default"
        assert_db_healthy(conn)
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """A second migrate() on an already-current DB is a no-op, not an error."""
    conn = init_db(tmp_path / "state.db")
    try:
        assert migrate(conn) == SCHEMA_VERSION
        assert migrate(conn) == SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM projects;").fetchone()[0] == 1
    finally:
        conn.close()


# ---- projects module ----


def test_register_and_list_projects(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        proj = register_project(conn, name="Quality Cat", sut_root="sites/qc")
        assert proj["id"] == "quality-cat"
        assert proj["sut_root"] == "sites/qc"
        assert get_project(conn, "quality-cat") == proj

        ids = [p["id"] for p in list_projects(conn)]
        # default is seeded first, then the registered one (created_at order).
        assert ids == [DEFAULT_PROJECT_ID, "quality-cat"]
    finally:
        conn.close()


def test_register_rejects_duplicate_and_bad_id(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        register_project(conn, name="alpha", sut_root=".")
        with pytest.raises(UsageError, match="already exists"):
            register_project(conn, name="alpha", sut_root=".")
        with pytest.raises(UsageError, match="must be lowercase slug"):
            register_project(conn, name="x", sut_root=".", project_id="Bad ID!")
        with pytest.raises(UsageError):
            register_project(conn, name="  ", sut_root=".")
    finally:
        conn.close()


def test_ensure_default_project_reconciles_sut_root(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        # Seeded by the migration with the config-blind placeholder '.'.
        assert get_project(conn, DEFAULT_PROJECT_ID)["sut_root"] == "."
        ensure_default_project(conn, sut_root="apps/web")
        assert get_project(conn, DEFAULT_PROJECT_ID)["sut_root"] == "apps/web"
        # Idempotent and still a single default row.
        ensure_default_project(conn, sut_root="apps/web")
        assert sum(1 for p in list_projects(conn) if p["id"] == DEFAULT_PROJECT_ID) == 1
    finally:
        conn.close()


def test_resolve_active_project_precedence(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "state.db")
    try:
        register_project(conn, name="beta", sut_root=".")
        # No config -> default.
        assert resolve_active_project_id(conn) == DEFAULT_PROJECT_ID
        # Config names an existing project.
        cfg = _Cfg({"project": {"active": "beta"}})
        assert resolve_active_project_id(conn, cfg) == "beta"
        # Explicit flag beats config.
        assert resolve_active_project_id(conn, cfg, explicit=DEFAULT_PROJECT_ID) == DEFAULT_PROJECT_ID
        # Unknown explicit / config -> operator error.
        with pytest.raises(UsageError, match="unknown project"):
            resolve_active_project_id(conn, explicit="ghost")
        with pytest.raises(UsageError, match="project.active"):
            resolve_active_project_id(conn, _Cfg({"project": {"active": "ghost"}}))
    finally:
        conn.close()


# ---- work_items scoping ----


def _make_item(conn, paths, title: str, **kwargs) -> str:
    detail = create_work_item_from_payload(
        conn,
        paths,
        EventLog(conn, paths),
        {"title": title, "priority": "P2", "business_goal": "g", "expected_behavior": "b"},
        **kwargs,
    )
    return detail["work_item"]["id"]


def test_new_work_item_lands_on_default_project(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        wid = _make_item(conn, paths, "zero-config task")
        assert get_work_item(conn, wid)["project_id"] == DEFAULT_PROJECT_ID
    finally:
        conn.close()


def test_work_items_isolated_by_project(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths.ensure()
    conn = init_db(paths.db)
    try:
        register_project(conn, name="alpha", sut_root=".")
        default_wid = _make_item(conn, paths, "default task")
        alpha_wid = _make_item(conn, paths, "alpha task", project_id="alpha")

        alpha_items = list_work_items(conn, project_id="alpha")
        assert [i["id"] for i in alpha_items] == [alpha_wid]

        default_items = list_work_items(conn, project_id=DEFAULT_PROJECT_ID)
        assert [i["id"] for i in default_items] == [default_wid]

        # No filter still returns every row (zero-config single-SUT view).
        assert {i["id"] for i in list_work_items(conn)} == {default_wid, alpha_wid}
    finally:
        conn.close()


# ---- config + CLI ----

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_project_config_block_validates_and_rejects_bad_types(tmp_path: Path) -> None:
    from agentic_os.config import load_config
    from agentic_os.errors import ConfigError

    base = (REPO_ROOT / "config" / "agentic-os.yml.example").read_text(encoding="utf-8")
    good = tmp_path / "good.yml"
    good.write_text(base + "\nproject:\n  active: default\n", encoding="utf-8")
    cfg = load_config(good)
    assert cfg.raw["project"]["active"] == "default"

    bad = tmp_path / "bad.yml"
    bad.write_text(base + "\nproject:\n  active: 123\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_project_cli_register_and_list(tmp_path: Path, capsys) -> None:
    from agentic_os.cli import cmd_project

    (tmp_path / ".git").mkdir()
    rc = cmd_project(tmp_path, ["register", "Gamma", "--sut-root", "apps/g"], json_output=True)
    assert rc == 0
    registered = json.loads(capsys.readouterr().out)["registered"]
    assert registered["id"] == "gamma"

    rc = cmd_project(tmp_path, ["list"], json_output=True)
    assert rc == 0
    ids = {p["id"] for p in json.loads(capsys.readouterr().out)["projects"]}
    assert ids == {DEFAULT_PROJECT_ID, "gamma"}

    rc = cmd_project(tmp_path, ["show", "gamma"], json_output=True)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["sut_root"] == "apps/g"


def _write_config(repo_root: Path, *, active: str | None = None) -> None:
    base = (REPO_ROOT / "config" / "agentic-os.yml.example").read_text(encoding="utf-8")
    if active is not None:
        base += f"\nproject:\n  active: {active}\n"
    cfg_dir = repo_root / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agentic-os.yml").write_text(base, encoding="utf-8")


def test_task_create_routes_to_active_project(tmp_path: Path, capsys) -> None:
    from agentic_os.cli import cmd_project, cmd_task

    (tmp_path / ".git").mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("# A task\n\nPriority: P2\n\nBody.\n", encoding="utf-8")

    cmd_project(tmp_path, ["register", "Delta", "--sut-root", "."], json_output=True)
    capsys.readouterr()

    # Config project.active routes the new work item.
    _write_config(tmp_path, active="delta")
    rc = cmd_task(tmp_path, ["create", "spec.md"], json_output=True)
    assert rc == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["work_item"]["project_id"] == "delta"

    # Explicit --project beats config.
    rc = cmd_task(tmp_path, ["create", "spec.md", "--project", DEFAULT_PROJECT_ID], json_output=True)
    assert rc == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["work_item"]["project_id"] == DEFAULT_PROJECT_ID
