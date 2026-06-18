"""SQLite WAL connection + migration runner."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from ..time_utils import now_iso

SCHEMA_VERSION = 16
SCHEMA_NAME = "coverage_ledger"
_SCHEMA_FILE = Path(__file__).parent / "schema.sql"

_MIGRATIONS = {
    2: (
        "work_items",
        """
        CREATE TABLE IF NOT EXISTS work_items (
          id          TEXT PRIMARY KEY,
          title       TEXT NOT NULL,
          status      TEXT NOT NULL CHECK (status IN (
            'draft','queued','analyzing','planned','implementing','reviewing',
            'running','bug_adjudication','blocked','done','failed'
          )),
          spec_path   TEXT NOT NULL,
          sut_root    TEXT NOT NULL,
          priority    TEXT NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
          created_at  TEXT NOT NULL,
          updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS work_item_artifacts (
          id            TEXT PRIMARY KEY,
          work_item_id  TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
          kind          TEXT NOT NULL CHECK (kind IN (
            'spec','sut_map','analysis','test_plan','patch','gate','run','bug','report','evidence'
          )),
          path          TEXT NOT NULL,
          created_at    TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS work_items_status_priority_idx
          ON work_items(status, priority, created_at);

        CREATE INDEX IF NOT EXISTS work_item_artifacts_work_item_idx
          ON work_item_artifacts(work_item_id, kind, created_at);
        """,
    ),
    # Issue #87 — add `apply` artifact kind to record that a reviewed
    # patch reached the working tree. SQLite cannot ALTER a CHECK
    # constraint, so the table is rebuilt via the temp-swap pattern.
    3: (
        "work_item_artifacts_apply_kind",
        """
        DROP INDEX IF EXISTS work_item_artifacts_work_item_idx;

        CREATE TABLE work_item_artifacts_new (
          id            TEXT PRIMARY KEY,
          work_item_id  TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
          kind          TEXT NOT NULL CHECK (kind IN (
            'spec','sut_map','analysis','test_plan','patch','gate','apply','run','bug','report','evidence'
          )),
          path          TEXT NOT NULL,
          created_at    TEXT NOT NULL
        );

        INSERT INTO work_item_artifacts_new(id, work_item_id, kind, path, created_at)
          SELECT id, work_item_id, kind, path, created_at FROM work_item_artifacts;

        DROP TABLE work_item_artifacts;
        ALTER TABLE work_item_artifacts_new RENAME TO work_item_artifacts;

        CREATE INDEX IF NOT EXISTS work_item_artifacts_work_item_idx
          ON work_item_artifacts(work_item_id, kind, created_at);
        """,
    ),
    # Issue #102 — model_invocations.provider/model_role CHECK was
    # narrower than the config validator, so the default triager
    # (antigravity/gemini) failed to record. Rebuild with the union.
    4: (
        "model_invocations_extended_providers",
        """
        DROP INDEX IF EXISTS model_invocations_task_idx;

        CREATE TABLE model_invocations_new (
          id             TEXT PRIMARY KEY,
          task_id         TEXT REFERENCES tasks(id) ON DELETE SET NULL,
          run_id          TEXT REFERENCES runs(id) ON DELETE SET NULL,
          model_role      TEXT NOT NULL CHECK (model_role IN (
            'opus','sonnet','codex','script','gemini','antigravity','haiku','triager'
          )),
          provider        TEXT NOT NULL CHECK (provider IN (
            'claude','codex','script','antigravity','gemini'
          )),
          command         TEXT NOT NULL CHECK (json_valid(command)),
          input_path      TEXT,
          output_path     TEXT,
          exit_code       INTEGER,
          started_at      TEXT NOT NULL,
          finished_at     TEXT,
          decision_id     TEXT REFERENCES decisions(id)
        );

        INSERT INTO model_invocations_new(
          id, task_id, run_id, model_role, provider, command,
          input_path, output_path, exit_code, started_at, finished_at, decision_id
        ) SELECT id, task_id, run_id, model_role, provider, command,
                 input_path, output_path, exit_code, started_at, finished_at, decision_id
            FROM model_invocations;

        DROP TABLE model_invocations;
        ALTER TABLE model_invocations_new RENAME TO model_invocations;

        CREATE INDEX IF NOT EXISTS model_invocations_task_idx
          ON model_invocations(task_id);
        """,
    ),
    5: (
        "model_invocations_envelope_budget",
        """
        ALTER TABLE model_invocations ADD COLUMN session_id TEXT;
        ALTER TABLE model_invocations ADD COLUMN provider_version TEXT;
        ALTER TABLE model_invocations ADD COLUMN tokens_in INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE model_invocations ADD COLUMN tokens_out INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE model_invocations ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0.0;
        """,
    ),
    6: (
        "workflow_idempotency_and_reviewer_leases",
        """
        ALTER TABLE runs ADD COLUMN idempotency_key TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS runs_idempotency_key_uq
          ON runs(idempotency_key)
          WHERE idempotency_key IS NOT NULL;

        ALTER TABLE work_items ADD COLUMN reviewer_lease TEXT;
        ALTER TABLE work_items ADD COLUMN reviewer_lease_expires TEXT;
        """,
    ),
    # Issue #235 — provider cooldown registry. Rows mark a provider cold
    # after a rate-limit / quota / auth signal so subsequent invocations
    # for the same role skip the cold provider until cooldown_until passes.
    # Survives orchestrator restart.
    7: (
        "provider_cooldowns",
        """
        CREATE TABLE IF NOT EXISTS provider_cooldowns (
          role            TEXT NOT NULL,
          provider        TEXT NOT NULL,
          cooldown_until  TEXT NOT NULL,
          trigger         TEXT NOT NULL,
          updated_at      TEXT NOT NULL,
          PRIMARY KEY (role, provider)
        );
        """,
    ),
    # Issue #247 — the decisions.decided_by CHECK only allows model roles
    # (opus/sonnet/codex/operator/script). The Verifications view needs to
    # distinguish autonomous decisions (planner-autopilot, triager-autopilot)
    # from operator overrides. A dedicated `actor` column carries the full
    # identity; `decided_by` keeps its constrained model-role semantics.
    # Backfilled from decided_by for existing rows.
    8: (
        "decisions_actor_column",
        """
        ALTER TABLE decisions ADD COLUMN actor TEXT;
        UPDATE decisions SET actor = decided_by WHERE actor IS NULL;
        """,
    ),
    # Issue #269 — durable autonomy session index + operator bookmarks.
    # The live session lives in-process; this table is the post-hoc audit
    # record the /sessions history + replay views read. Counts are written
    # at session end from the in-memory events_log (events NDJSON carries no
    # session id, so it cannot be grouped after the fact).
    9: (
        "autonomy_sessions_and_bookmarks",
        """
        CREATE TABLE IF NOT EXISTS autonomy_sessions (
          id                    TEXT PRIMARY KEY,
          started_at            TEXT NOT NULL,
          finished_at           TEXT,
          status                TEXT NOT NULL,
          mode                  TEXT NOT NULL,
          max_minutes           INTEGER,
          work_items_processed  INTEGER NOT NULL DEFAULT 0,
          blocks                INTEGER NOT NULL DEFAULT 0,
          failures              INTEGER NOT NULL DEFAULT 0,
          primary_actor         TEXT
        );

        CREATE TABLE IF NOT EXISTS session_bookmarks (
          session_id  TEXT PRIMARY KEY REFERENCES autonomy_sessions(id) ON DELETE CASCADE,
          label       TEXT NOT NULL,
          created_at  TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS autonomy_sessions_started_idx
          ON autonomy_sessions(started_at);
        """,
    ),
    # Issue #270 — structured reasoning transcript per model invocation.
    # kind ∈ {thinking, tool_call, tool_result, text, error}. Payloads pass
    # the secret-redaction filter before they land here.
    10: (
        "model_transcripts",
        """
        CREATE TABLE IF NOT EXISTS model_transcripts (
          invocation_id  TEXT NOT NULL,
          kind           TEXT NOT NULL,
          ord            INTEGER NOT NULL,
          payload        TEXT NOT NULL,
          ts             TEXT NOT NULL,
          PRIMARY KEY (invocation_id, ord)
        );
        """,
    ),
    # Issue #271 — cron-style schedules for autonomous runs. Rows are fired
    # by the ScheduleRunner thread that polls this table while `up --daemon`
    # serves; `last_run`/`last_status` carry the most recent invocation.
    11: (
        "schedules",
        """
        CREATE TABLE IF NOT EXISTS schedules (
          name         TEXT PRIMARY KEY,
          cron         TEXT NOT NULL,
          action       TEXT NOT NULL,
          enabled      INTEGER NOT NULL DEFAULT 1,
          last_run     TEXT,
          last_status  TEXT
        );
        """,
    ),
    # Issue #274 — dependency edges between work items. `parent` must reach
    # status `done` before `child` becomes selectable under the DEPENDENCY /
    # HYBRID queue policies. Edges cascade away when either work item is
    # deleted. `kind` is reserved for future edge semantics (default 'blocks').
    12: (
        "work_item_deps",
        """
        CREATE TABLE IF NOT EXISTS work_item_deps (
          parent_id   TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
          child_id    TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
          kind        TEXT NOT NULL DEFAULT 'blocks',
          created_at  TEXT NOT NULL,
          PRIMARY KEY (parent_id, child_id)
        );

        CREATE INDEX IF NOT EXISTS work_item_deps_child_idx
          ON work_item_deps(child_id);
        """,
    ),
    # Issue #273 — cross-run learnings store. Advisory hints (flaky scenarios,
    # skill failure rates, provider quality, coverage gaps) consulted by the
    # planner + provider router; gates still decide. One row per (kind,
    # subject) via the UNIQUE index; `weight` decays nightly via the scheduler.
    13: (
        "learnings",
        """
        CREATE TABLE IF NOT EXISTS learnings (
          id           INTEGER PRIMARY KEY,
          kind         TEXT NOT NULL CHECK (kind IN (
            'flaky','skill_failure','provider_quality','coverage_gap'
          )),
          subject      TEXT NOT NULL,
          payload      TEXT NOT NULL,
          observed_at  TEXT NOT NULL,
          weight       REAL NOT NULL,
          actor        TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS learnings_kind_subject_idx
          ON learnings(kind, subject);
        """,
    ),
    # Issue #288 — addressable projects over the flat work_items list. The
    # runtime was single-SUT-per-checkout; per-project RAG memory (#289) needs
    # projects to be first-class so history/context can be scoped. A literal
    # 'default' project is seeded and every existing work_item / session /
    # learning is backfilled onto it, so an existing single-SUT runtime keeps
    # working with zero behaviour change. `project_id` is nullable on the child
    # tables (NULL never violates the FK) and the read-side filters stay opt-in;
    # this migration only establishes the addressable boundary #289 attaches to.
    # The default project's sut_root is reconciled from the live config at
    # runtime (projects.ensure_default_project) — migrations stay config-blind.
    14: (
        "projects",
        """
        CREATE TABLE IF NOT EXISTS projects (
          id          TEXT PRIMARY KEY,
          name        TEXT NOT NULL,
          sut_root    TEXT NOT NULL,
          config_ref  TEXT,
          created_at  TEXT NOT NULL
        );

        INSERT OR IGNORE INTO projects(id, name, sut_root, config_ref, created_at)
          VALUES ('default', 'default', '.', NULL,
                  strftime('%Y-%m-%dT%H:%M:%fZ','now'));

        ALTER TABLE work_items ADD COLUMN project_id TEXT REFERENCES projects(id);
        ALTER TABLE autonomy_sessions ADD COLUMN project_id TEXT REFERENCES projects(id);
        ALTER TABLE learnings ADD COLUMN project_id TEXT REFERENCES projects(id);

        UPDATE work_items SET project_id = 'default' WHERE project_id IS NULL;
        UPDATE autonomy_sessions SET project_id = 'default' WHERE project_id IS NULL;
        UPDATE learnings SET project_id = 'default' WHERE project_id IS NULL;

        CREATE INDEX IF NOT EXISTS work_items_project_idx
          ON work_items(project_id);
        """,
    ),
    # Issue #289 — per-project RAG memory. A standalone, denormalized FTS5
    # index: each row is one indexed source artifact (summary / transcript /
    # bug / decision / learning) scoped to a project_id. Zero new deps —
    # SQLite FTS5 (porter unicode61) does the semantic recall. `build_memory`
    # rebuilds it from canonical sources; nothing else writes here.
    15: (
        "memory_index",
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_index USING fts5(
          project_id UNINDEXED,
          source UNINDEXED,
          source_id UNINDEXED,
          ts UNINDEXED,
          title,
          body,
          tokenize = 'porter unicode61'
        );
        """,
    ),
    # Issue #319 (Wave 12) — persistent per-SUT/project coverage ledger. One
    # row per covered surface (route/endpoint + assertion kind), idempotent on
    # (project_id, surface_kind, surface_key, assertion_kind) so #320 can gate
    # accumulation on "is surface X already covered?".
    16: (
        "coverage_ledger",
        """
        CREATE TABLE IF NOT EXISTS coverage_ledger (
          id             TEXT PRIMARY KEY,
          project_id     TEXT NOT NULL REFERENCES projects(id),
          surface_kind   TEXT NOT NULL CHECK (surface_kind IN ('api','ui')),
          surface_key    TEXT NOT NULL,
          assertion_kind TEXT NOT NULL,
          spec_path      TEXT NOT NULL,
          candidate_id   TEXT,
          work_item_id   TEXT,
          run_id         TEXT,
          created_at     TEXT NOT NULL,
          updated_at     TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS coverage_ledger_surface_idx
          ON coverage_ledger(project_id, surface_kind, surface_key, assertion_kind);

        CREATE INDEX IF NOT EXISTS coverage_ledger_project_idx
          ON coverage_ledger(project_id, updated_at);
        """,
    ),
    # Issue #339 — `model_invocations.task_id` FK targets `tasks(id)`, a
    # different schema slice than the autonomous pipeline's `work_items(id)`.
    # After #308 wired the autonomous loop into `invoke_model`, the FK
    # forced every pipeline row to record with `task_id=NULL`, breaking the
    # canonical id chain. This migration adds an explicit `work_item_id`
    # column with a direct FK to `work_items(id)` so the autonomous-pipeline
    # rows resolve at the SQL level. The legacy `task_id` column stays for
    # the older execution path (runs.task_id chain) — both can be set in
    # parallel without conflict.
    17: (
        "model_invocations_work_item_id",
        """
        ALTER TABLE model_invocations
          ADD COLUMN work_item_id TEXT REFERENCES work_items(id) ON DELETE SET NULL;

        CREATE INDEX IF NOT EXISTS model_invocations_work_item_idx
          ON model_invocations(work_item_id);
        """,
    ),
}


# Issue #362 — bounded retry for the WAL-mode flip under concurrent opens.
_CONNECT_WAL_ATTEMPTS = 12
_CONNECT_WAL_BASE_SLEEP = 0.005


def _set_wal_mode(conn: sqlite3.Connection) -> None:
    """Enable WAL, retrying on the lock contention from concurrent opens.

    Under the #361 doctrine every fan-out worker owns its **own** connection to
    the shared database, so N connections call ``connect()`` at once. The first
    flips the journal from ``delete`` to ``wal``, which needs an EXCLUSIVE lock;
    SQLite returns an immediate ``database is locked`` to the concurrent openers
    and bypasses ``busy_timeout`` on this path (the timer is skipped for the
    mode switch to avoid deadlock). A small capped backoff lets the winner's
    flip settle so every opener ends up in WAL instead of one crashing on open.
    """
    last_exc: Optional[sqlite3.OperationalError] = None
    for attempt in range(_CONNECT_WAL_ATTEMPTS):
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            return
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last_exc = exc
            time.sleep(_CONNECT_WAL_BASE_SLEEP * (2 ** min(attempt, 5)))
    assert last_exc is not None  # the loop only exits the except branch with it set
    raise last_exc


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # busy_timeout MUST precede any lock-taking PRAGMA so they honor it (the
    # connect() timeout installs a busy handler, but setting the pragma
    # explicitly first is belt-and-suspenders and self-documenting).
    conn.execute("PRAGMA busy_timeout=5000;")
    _set_wal_mode(conn)
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA wal_autocheckpoint=1000;")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    else:
        conn.execute("COMMIT;")


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations';"
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations;").fetchone()
    return int(row["v"])


def integrity_check(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check;").fetchone()
    return row[0] if row else "unknown"


def foreign_key_violations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("PRAGMA foreign_key_check;").fetchall())


def assert_db_healthy(conn: sqlite3.Connection) -> None:
    integrity = integrity_check(conn)
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
    violations = foreign_key_violations(conn)
    if violations:
        raise RuntimeError(f"SQLite foreign_key_check failed: {len(violations)} violation(s)")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply schema up to SCHEMA_VERSION. Returns the version reached."""
    version = current_version(conn)
    if version >= SCHEMA_VERSION:
        assert_db_healthy(conn)
        return version
    if version == 0:
        sql = _SCHEMA_FILE.read_text(encoding="utf-8")
        # executescript manages its own transaction on autocommit connections, so it
        # must run outside of our BEGIN/COMMIT wrapper.
        conn.executescript(sql)
        with transaction(conn):
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?);",
                (1, "initial", now_iso()),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?);",
                (SCHEMA_VERSION, SCHEMA_NAME, now_iso()),
            )
        assert_db_healthy(conn)
        return SCHEMA_VERSION

    for next_version in range(version + 1, SCHEMA_VERSION + 1):
        name, sql = _MIGRATIONS[next_version]
        conn.executescript(sql)
        with transaction(conn):
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?);",
                (next_version, name, now_iso()),
            )
    assert_db_healthy(conn)
    return SCHEMA_VERSION


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn
