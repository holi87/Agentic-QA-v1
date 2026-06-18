-- Agentic OS canonical schema (migration version 16).
-- Source of truth: docs/database-schema.md. Do not edit names or types without an ADR.
-- This file is the fresh-install path: db.py runs it verbatim and stamps
-- straight to SCHEMA_VERSION, so it must stay a superset of every migration.

CREATE TABLE IF NOT EXISTS schema_migrations (
  version       INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  applied_at    TEXT NOT NULL
);

-- Issue #288 — addressable projects over the flat work_items list. A literal
-- 'default' project is always present so single-SUT runtimes keep working with
-- zero config. Its sut_root is reconciled from the live config at runtime.
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
  decided_at    TEXT NOT NULL,
  -- Issue #247 — full decision identity (e.g. planner-autopilot,
  -- triager-autopilot, operator). decided_by stays the constrained model
  -- role; actor distinguishes autonomous decisions from operator overrides.
  actor         TEXT
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
  work_item_id    TEXT REFERENCES work_items(id) ON DELETE SET NULL,
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
  project_id  TEXT REFERENCES projects(id),
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

CREATE TABLE IF NOT EXISTS provider_cooldowns (
  role            TEXT NOT NULL,
  provider        TEXT NOT NULL,
  cooldown_until  TEXT NOT NULL,
  trigger         TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (role, provider)
);

-- Issue #269 — durable autonomy session index + operator bookmarks.
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
  primary_actor         TEXT,
  project_id            TEXT REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS session_bookmarks (
  session_id  TEXT PRIMARY KEY REFERENCES autonomy_sessions(id) ON DELETE CASCADE,
  label       TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

-- Issue #270 — structured reasoning transcript per model invocation.
CREATE TABLE IF NOT EXISTS model_transcripts (
  invocation_id  TEXT NOT NULL,
  kind           TEXT NOT NULL,
  ord            INTEGER NOT NULL,
  payload        TEXT NOT NULL,
  ts             TEXT NOT NULL,
  PRIMARY KEY (invocation_id, ord)
);

-- Issue #271 — cron-style schedules for autonomous runs.
CREATE TABLE IF NOT EXISTS schedules (
  name         TEXT PRIMARY KEY,
  cron         TEXT NOT NULL,
  action       TEXT NOT NULL,
  enabled      INTEGER NOT NULL DEFAULT 1,
  last_run     TEXT,
  last_status  TEXT
);

-- Issue #274 — dependency edges between work items (parent must be `done`
-- before child is selectable under DEPENDENCY / HYBRID queue policies).
CREATE TABLE IF NOT EXISTS work_item_deps (
  parent_id   TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
  child_id    TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL DEFAULT 'blocks',
  created_at  TEXT NOT NULL,
  PRIMARY KEY (parent_id, child_id)
);

CREATE INDEX IF NOT EXISTS work_item_deps_child_idx
  ON work_item_deps(child_id);

-- Issue #273 — cross-run learnings store. Advisory hints only; gates still
-- decide. One row per (kind, subject); re-observe resets observed_at and
-- weight (recency over frequency). `weight` decays nightly via the scheduler.
CREATE TABLE IF NOT EXISTS learnings (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL CHECK (kind IN (
    'flaky','skill_failure','provider_quality','coverage_gap'
  )),
  subject      TEXT NOT NULL,
  payload      TEXT NOT NULL,
  observed_at  TEXT NOT NULL,
  weight       REAL NOT NULL,
  actor        TEXT NOT NULL,
  project_id   TEXT REFERENCES projects(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS learnings_kind_subject_idx
  ON learnings(kind, subject);

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

CREATE UNIQUE INDEX IF NOT EXISTS runs_idempotency_key_uq
  ON runs(idempotency_key)
  WHERE idempotency_key IS NOT NULL;

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

CREATE INDEX IF NOT EXISTS model_invocations_work_item_idx
  ON model_invocations(work_item_id);

CREATE INDEX IF NOT EXISTS assertion_changes_status_idx
  ON assertion_changes(status);

CREATE INDEX IF NOT EXISTS work_items_status_priority_idx
  ON work_items(status, priority, created_at);

CREATE INDEX IF NOT EXISTS work_item_artifacts_work_item_idx
  ON work_item_artifacts(work_item_id, kind, created_at);

CREATE INDEX IF NOT EXISTS work_items_project_idx
  ON work_items(project_id);

CREATE INDEX IF NOT EXISTS events_ts_idx
  ON events(ts);

CREATE INDEX IF NOT EXISTS events_phase_idx
  ON events(phase_id, ts);

CREATE INDEX IF NOT EXISTS events_run_idx
  ON events(run_id, ts);

CREATE INDEX IF NOT EXISTS autonomy_sessions_started_idx
  ON autonomy_sessions(started_at);

-- Issue #289 — per-project RAG memory. Standalone denormalized FTS5 index;
-- one row per indexed source artifact, scoped to project_id. Rebuilt from
-- canonical sources by memory.build_memory (nothing else writes here).
CREATE VIRTUAL TABLE IF NOT EXISTS memory_index USING fts5(
  project_id UNINDEXED,
  source UNINDEXED,
  source_id UNINDEXED,
  ts UNINDEXED,
  title,
  body,
  tokenize = 'porter unicode61'
);

-- Issue #319 (Wave 12) — persistent per-SUT/project coverage ledger. One row
-- per covered surface (route/endpoint + assertion kind); idempotent on
-- (project_id, surface_kind, surface_key, assertion_kind) so #320 can gate
-- accumulation on "is surface X already covered?".
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
