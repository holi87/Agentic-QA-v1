# Agentic OS database schema

Status: active

- Contract gate: Accepted for implementation
- Phase: `phase/10-codex-dashboard-task-intake`
- Storage: SQLite 3 in WAL mode

This file is the canonical runtime schema after phase 10. The
implementation should copy the DDL into
`scripts/agentic-os/agentic_os/storage/schema.sql` without changing table
or column names.

## 1. Connection pragmas

Every write / read-write connection must execute:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

The runtime stores timestamps as ISO-8601 UTC text (`YYYY-MM-DDTHH:MM:SS.sssZ`). Runtime identifiers are textual: ULID for tasks/decisions/events and `run-<ULID>` for runs.

## 2. Canonical DDL

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  version       INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  applied_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS phases (
  id            TEXT PRIMARY KEY,
  status        TEXT NOT NULL CHECK (status IN ('planned','in_progress','blocked','interrupted','done','aborted')),
  branch        TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT,
  exit_summary  TEXT,
  spec_path     TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  phase_id      TEXT NOT NULL REFERENCES phases(id) ON DELETE CASCADE,
  parent_id     TEXT REFERENCES tasks(id),
  kind          TEXT NOT NULL CHECK (kind IN ('run','review','decision','bug','doc','recovery')),
  status        TEXT NOT NULL CHECK (status IN ('queued','leased','running','succeeded','failed','cancelled','timeout')),
  payload       TEXT NOT NULL CHECK (json_valid(payload)),
  lease_owner   TEXT,
  lease_expires TEXT,
  created_at    TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT,
  exit_code     INTEGER,
  error_class   TEXT,
  retry_of      TEXT REFERENCES tasks(id),
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  idempotency_key TEXT,
  command       TEXT NOT NULL CHECK (json_valid(command)),
  cwd           TEXT NOT NULL,
  env_hash      TEXT NOT NULL,
  exit_code     INTEGER,
  duration_ms   INTEGER,
  log_path      TEXT NOT NULL,
  evidence_path TEXT,
  manifest_path TEXT,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  failure_kind  TEXT CHECK (failure_kind IN ('product','infra','timeout','user_abort','unknown') OR failure_kind IS NULL),
  unmapped_exit INTEGER NOT NULL DEFAULT 0 CHECK (unmapped_exit IN (0,1))
);

CREATE TABLE IF NOT EXISTS decisions (
  id            TEXT PRIMARY KEY,
  phase_id      TEXT NOT NULL REFERENCES phases(id),
  topic         TEXT NOT NULL,
  decided_by    TEXT NOT NULL CHECK (decided_by IN ('opus','sonnet','codex','operator','script')),
  rationale     TEXT NOT NULL,
  consequences  TEXT NOT NULL,
  reversed_by   TEXT REFERENCES decisions(id),
  decided_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blockers (
  id            TEXT PRIMARY KEY,
  phase_id      TEXT NOT NULL REFERENCES phases(id),
  severity      TEXT NOT NULL CHECK (severity IN ('P0','P1','P2','P3')),
  source        TEXT NOT NULL,
  description   TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('open','mitigated','accepted','closed')),
  opened_at     TEXT NOT NULL,
  closed_at     TEXT
);

CREATE TABLE IF NOT EXISTS bugs (
  id            TEXT PRIMARY KEY,
  scenario_tag  TEXT NOT NULL,
  severity      TEXT NOT NULL CHECK (severity IN ('P0','P1','P2','P3')),
  status        TEXT NOT NULL CHECK (status IN ('open','known','fixed','wont_fix')),
  evidence_dir  TEXT NOT NULL,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  decided_by    TEXT REFERENCES decisions(id)
);

CREATE TABLE IF NOT EXISTS test_results (
  id              TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  bug_id          TEXT REFERENCES bugs(id),
  scenario_name   TEXT NOT NULL,
  feature_path    TEXT NOT NULL,
  line            INTEGER,
  status          TEXT NOT NULL CHECK (status IN ('passed','failed','skipped','pending','undefined','ambiguous')),
  duration_ms     INTEGER,
  functional_tag  TEXT NOT NULL,
  lifecycle_tag   TEXT,
  all_tags        TEXT NOT NULL CHECK (json_valid(all_tags)),
  failure_message TEXT,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
  id            TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  bug_id        TEXT REFERENCES bugs(id),
  kind          TEXT NOT NULL CHECK (kind IN ('manifest','screenshot','trace','allure','junit','cucumber_html','summary','log','patch','other')),
  path          TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_invocations (
  id             TEXT PRIMARY KEY,
  session_id     TEXT,
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
  provider_version TEXT,
  tokens_in       INTEGER NOT NULL DEFAULT 0,
  tokens_out      INTEGER NOT NULL DEFAULT 0,
  cost_usd        REAL NOT NULL DEFAULT 0.0,
  decision_id     TEXT REFERENCES decisions(id)
);

CREATE TABLE IF NOT EXISTS assertion_changes (
  id              TEXT PRIMARY KEY,
  task_id          TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  run_id           TEXT REFERENCES runs(id) ON DELETE SET NULL,
  file_path        TEXT NOT NULL,
  assertion_before TEXT NOT NULL,
  assertion_after  TEXT NOT NULL,
  classification   TEXT NOT NULL CHECK (classification IN ('strengthened','unchanged','weakened','unknown')),
  decision_id      TEXT REFERENCES decisions(id),
  status           TEXT NOT NULL CHECK (status IN ('allowed','blocked','needs_decision')),
  created_at       TEXT NOT NULL
);

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
  reviewer_lease TEXT,
  reviewer_lease_expires TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_item_artifacts (
  id            TEXT PRIMARY KEY,
  work_item_id  TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL CHECK (kind IN (
    'spec','sut_map','analysis','test_plan','patch','gate','apply','run','bug','report','evidence'
  )),
  path          TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id            TEXT PRIMARY KEY,
  ts            TEXT NOT NULL,
  run_id        TEXT REFERENCES runs(id) ON DELETE SET NULL,
  phase_id      TEXT REFERENCES phases(id) ON DELETE SET NULL,
  task_id       TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  kind          TEXT NOT NULL,
  actor         TEXT NOT NULL,
  severity      TEXT NOT NULL CHECK (severity IN ('info','warning','error')),
  payload       TEXT NOT NULL CHECK (json_valid(payload)),
  ndjson_file   TEXT,
  ndjson_offset INTEGER
);

CREATE TABLE IF NOT EXISTS leases (
  owner         TEXT PRIMARY KEY,
  pid           INTEGER NOT NULL,
  host          TEXT NOT NULL,
  acquired_at   TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  heartbeat_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_offsets (
  consumer      TEXT PRIMARY KEY,
  ndjson_file   TEXT NOT NULL,
  byte_offset   INTEGER NOT NULL,
  updated_at    TEXT NOT NULL
);
```

## 3. Required indexes

```sql
CREATE INDEX IF NOT EXISTS tasks_phase_status_idx
  ON tasks(phase_id, status);

CREATE INDEX IF NOT EXISTS tasks_lease_idx
  ON tasks(lease_owner, lease_expires);

CREATE INDEX IF NOT EXISTS tasks_retry_idx
  ON tasks(retry_of);

CREATE INDEX IF NOT EXISTS runs_task_idx
  ON runs(task_id);

CREATE INDEX IF NOT EXISTS runs_finished_idx
  ON runs(finished_at);

CREATE INDEX IF NOT EXISTS blockers_phase_status_idx
  ON blockers(phase_id, status);

CREATE INDEX IF NOT EXISTS bugs_status_severity_idx
  ON bugs(status, severity);

CREATE INDEX IF NOT EXISTS test_results_run_status_idx
  ON test_results(run_id, status);

CREATE INDEX IF NOT EXISTS test_results_bug_idx
  ON test_results(bug_id);

CREATE INDEX IF NOT EXISTS evidence_run_idx
  ON evidence(run_id);

CREATE INDEX IF NOT EXISTS evidence_bug_idx
  ON evidence(bug_id);

CREATE INDEX IF NOT EXISTS model_invocations_task_idx
  ON model_invocations(task_id);

CREATE INDEX IF NOT EXISTS assertion_changes_status_idx
  ON assertion_changes(status);

CREATE INDEX IF NOT EXISTS work_items_status_priority_idx
  ON work_items(status, priority, created_at);

CREATE INDEX IF NOT EXISTS work_item_artifacts_work_item_idx
  ON work_item_artifacts(work_item_id, kind, created_at);

CREATE INDEX IF NOT EXISTS events_ts_idx
  ON events(ts);

CREATE INDEX IF NOT EXISTS events_phase_idx
  ON events(phase_id, ts);

CREATE INDEX IF NOT EXISTS events_run_idx
  ON events(run_id, ts);
```

## 4. State transition rules

`tasks.status` transitions:

```text
queued -> leased -> running -> succeeded
queued -> leased -> running -> failed
queued -> leased -> running -> timeout
queued -> leased -> running -> cancelled
queued -> cancelled
leased -> queued        # only after lease recovery before command spawn
running -> failed       # abandoned recovery
```

`phases.status` transitions:

```text
planned -> in_progress -> done
planned -> in_progress -> blocked
in_progress -> interrupted -> in_progress
in_progress -> aborted
blocked -> in_progress
blocked -> aborted
```

The runtime does not enforce these transitions with triggers in phase 03. They are enforced in the storage/repository layer and an event is written on every change.

## 5. Event write contract

Every state change in the tables `phases`, `tasks`, `runs`, `decisions`,
`blockers`, `bugs`, `test_results`, `evidence`, `model_invocations`,
`assertion_changes`, `work_items`, `work_item_artifacts`, `leases` must have
a corresponding entry in `events`.

Write order:

1. open the SQLite transaction;
2. write the domain change;
3. write the `events` entry with `ndjson_file=NULL`, `ndjson_offset=NULL`;
4. commit;
5. append a JSON line to `agentic-os-runtime/events/YYYY-MM-DD.ndjson`;
6. update `events.ndjson_file` and `events.ndjson_offset`.

If step 5 or 6 fails, the runtime does not roll back the domain change. It opens a P1 `events_desynced` blocker on the next recovery scan and tries to backfill the NDJSON from the `events` table.

## 6. Table semantics

Workflows such as `dry-run`, `sut_start` and `sut_stop` do not extend `tasks.kind`. They are recorded as `tasks.kind='run'` with `payload.workflow`.

`test_results` stores the outcome of a single Cucumber scenario. Each row must have exactly one `functional_tag` starting with `@functional-`. `lifecycle_tag` may be `NULL` only when the parser could not read it; such a row creates a P1 blocker.

`bugs.status='known'` does not mean a green test. A scenario with `@known-bug @bug-NNN` that is still failing creates `test_results.status='failed'`, links `bug_id`, and causes exit `1`.

`assertion_changes.classification='weakened'` requires a `decision_id`. A missing decision sets `status='blocked'` and opens a P0/P1 blocker per the phase 06 policy.

`model_invocations` records real CLI invocations of models. Decisions made without a model go in `decisions.decided_by='script'` or `operator`, without an artificial model row.

`work_items` stores operator-level test tasks visible in the dashboard.
Existing `tasks` remain runtime execution-level units. One `work_item` may
later spawn many rows in `tasks`, `runs`, `bugs`, and `evidence`.

`work_item_artifacts` registers reviewable files associated with a task,
starting with the spec `kind='spec'` copied into `agentic-os-runtime/task-specs/`.

## 7. Migration policy

Migrations are monotonic. Phase 03 started at version `1`, and phase 10 adds version `2` with `work_items` and `work_item_artifacts`. Rules:

- do not change the type or meaning of an existing column;
- do not remove columns without an ADR;
- adding a nullable column via migration is allowed;
- a breaking migration requires a new ADR and a restore-from-backup test;
- every migration writes a `db.migration_applied` event.

## 8. Seed data after phase 10

After `init` the runtime creates minimal phases:

```text
02-codex-runtime-contract
03-sonnet-core-runtime
04-codex-persistence-guards
05-sonnet-dashboard-runner
06-opus-bug-adjudication
07-sonnet-qualitycat-integration
08-codex-final-gates-hardening
09-sonnet-fake-sut-dry-run (cancelled/deferred)
10-codex-dashboard-task-intake
11-sonnet-dashboard-task-ui
12-sonnet-sut-analysis-test-planning
13-sonnet-codex-patch-generation-gated-merge
14-sonnet-dashboard-run-e2e
15-sonnet-final-fake-sut-dry-run
16-sonnet-dashboard-visual-polish
```

`phase_id` is the identifier of a historical phase (legacy values seeded by `Orchestrator.seed_phases()`). `branch` is the name of the implementation branch. Active STEP2 workflows no longer require `docs/phases/` files.
