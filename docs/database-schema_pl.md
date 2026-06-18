# Agentic OS database schema

Status: active

- Contract gate: Accepted for implementation
- Phase: `phase/10-codex-dashboard-task-intake`
- Storage: SQLite 3 in WAL mode

Ten plik jest kanonicznym schematem runtime po fazie 10. Implementacja
powinna skopiować DDL do
`scripts/agentic-os/agentic_os/storage/schema.sql` bez zmiany nazw tabel
i kolumn.

## 1. Connection pragmas

Każde połączenie write / read-write musi wykonać:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
```

Runtime przechowuje czasy jako ISO-8601 UTC text (`YYYY-MM-DDTHH:MM:SS.sssZ`). Identyfikatory runtime są tekstowe: ULID dla tasków/decyzji/eventów i `run-<ULID>` dla runów.

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

Przejścia `tasks.status`:

```text
queued -> leased -> running -> succeeded
queued -> leased -> running -> failed
queued -> leased -> running -> timeout
queued -> leased -> running -> cancelled
queued -> cancelled
leased -> queued        # only after lease recovery before command spawn
running -> failed       # abandoned recovery
```

Przejścia `phases.status`:

```text
planned -> in_progress -> done
planned -> in_progress -> blocked
in_progress -> interrupted -> in_progress
in_progress -> aborted
blocked -> in_progress
blocked -> aborted
```

Runtime nie egzekwuje tych przejść triggerami w fazie 03. Egzekwuje je w warstwie storage/repository i zapisuje event przy każdej zmianie.

## 5. Event write contract

Każda zmiana stanu w tabelach `phases`, `tasks`, `runs`, `decisions`,
`blockers`, `bugs`, `test_results`, `evidence`, `model_invocations`,
`assertion_changes`, `work_items`, `work_item_artifacts`, `leases` musi mieć
odpowiadający wpis w `events`.

Write order:

1. otwórz transakcję SQLite;
2. zapisz zmianę domenową;
3. zapisz wpis `events` z `ndjson_file=NULL`, `ndjson_offset=NULL`;
4. commit;
5. dopisz JSON line do `agentic-os-runtime/events/YYYY-MM-DD.ndjson`;
6. zaktualizuj `events.ndjson_file` i `events.ndjson_offset`.

Jeżeli krok 5 lub 6 padnie, runtime nie cofa zmiany domenowej. Tworzy blocker P1 `events_desynced` przy następnym recovery scan i próbuje uzupełnić NDJSON z tabeli `events`.

## 6. Table semantics

Workflowy takie jak `dry-run`, `sut_start` i `sut_stop` nie rozszerzają `tasks.kind`. Są zapisywane jako `tasks.kind='run'` z `payload.workflow`.

`test_results` przechowuje wynik pojedynczego scenariusza Cucumber. Każdy rekord musi mieć dokładnie jeden `functional_tag` zaczynający się od `@functional-`. `lifecycle_tag` może być `NULL` tylko wtedy, gdy parser nie umiał go odczytać; taki rekord tworzy blocker P1.

`bugs.status='known'` nie oznacza zielonego testu. Scenariusz z `@known-bug @bug-NNN`, który nadal failuje, tworzy `test_results.status='failed'`, linkuje `bug_id` i powoduje exit `1`.

`assertion_changes.classification='weakened'` wymaga `decision_id`. Brak decyzji ustawia `status='blocked'` i otwiera blocker P0/P1 według polityki fazy 06.

`model_invocations` zapisuje realne wywołania CLI modeli. Decyzje bez modelu zapisuje `decisions.decided_by='script'` albo `operator`, bez sztucznego wpisu modelowego.

`work_items` przechowuje operator-level zadania testowe widoczne w dashboardzie.
Istniejące `tasks` pozostają execution-level jednostkami runtime. Jeden
`work_item` może później utworzyć wiele rekordów w `tasks`, `runs`, `bugs` i
`evidence`.

`work_item_artifacts` rejestruje reviewable pliki związane z zadaniem,
zaczynając od speca `kind='spec'` skopiowanego do `agentic-os-runtime/task-specs/`.

## 7. Migration policy

Migracje są monotoniczne. Faza 03 zaczęła od wersji `1`, a faza 10 dodaje wersję `2` z `work_items` i `work_item_artifacts`. Zasady:

- nie zmieniaj typu ani znaczenia istniejącej kolumny;
- nie usuwaj kolumn bez ADR;
- dodanie nullable column jest dozwolone przez migrację;
- breaking migration wymaga nowego ADR i testu restore z backupu;
- każda migracja zapisuje event `db.migration_applied`.

## 8. Seed data after phase 10

Po `init` runtime tworzy minimalne fazy:

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

`phase_id` to identyfikator fazy historycznej (legacy seedowane przez `Orchestrator.seed_phases()`). `branch` to nazwa brancha implementacji. Aktywne workflowy STEP2 nie wymagają już plików `docs/phases/`.
