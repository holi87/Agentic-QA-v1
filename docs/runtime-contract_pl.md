# Agentic OS runtime contract

Status: active

- Contract gate: Accepted
- Depends on: `docs/cli-contract.md`, `docs/database-schema.md`

Ten dokument jest kontraktem wykonawczym runtime. Jeżeli ten dokument i `docs/cli-contract.md` są w konflikcie, CLI contract wygrywa dla nazw komend i kodów wyjścia.

## 1. Runtime boundary

Agentic OS jest lokalnym runtime'em Python 3.13+ uruchamianym przez cienki shim `scripts/agentic-os.sh`. Shim nie zawiera logiki workflow, tylko:

1. ustala katalog repo;
2. ustawia `PYTHONPATH=scripts/agentic-os`;
3. wykonuje `python -m agentic_os <args>`;
4. propaguje exit code procesu Pythona bez tłumaczenia.

Runtime pisze tylko do:

- `agentic-os-runtime/` jako prywatnego stanu operacyjnego;
- `reports/`, `bugs/`, `evidence/` jako artefaktów dostarczanych operatorowi;
- `config/agentic-os.yml`, jeżeli operator wywołał `init` i plik nie istnieje (`init` także migruje stary `.qualitycat/agentic-os.yml` do kanonicznej ścieżki);
- `config/agentic-os.yml.example`, templates i kodu repo w czasie implementacji frameworka.

Runtime nie pisze do SUT. Wyjątki są dokładnie dwa: `sandbox-sut/` w fazie 09 oraz instalacja shima `run-tests.sh` w SUT po jawnym `--install-shim`.

## 2. Directory contract

Kanoniczny runtime layout:

```text
agentic-os-runtime/
  state.db
  state.db-wal
  state.db-shm
  events/
    YYYY-MM-DD.ndjson
    current -> YYYY-MM-DD.ndjson
  logs/
    orchestrator.log
    dashboard.log
    subprocess/<run-id>.log
  patches/<phase-id>/<run-id>/
  worktree/<run-id>/
  evidence/<run-id>/
    manifest.json
  backups/
    state-YYYYMMDDTHHMMSS.db
    state-YYYYMMDDTHHMMSS.db.sha256
  leases/<owner>.json
  pids/
  tmp/
```

Publiczne artefakty:

```text
reports/
bugs/
evidence/
```

`agentic-os-runtime/evidence/<run-id>/manifest.json` jest źródłem prawdy dla plików runa. Root `evidence/` jest kopią handoff dla operatora i narzędzi QualityCat.

## 3. Configuration contract

Plik konfiguracji: `config/agentic-os.yml` (kanoniczny). `.qualitycat/agentic-os.yml` jest akceptowany jako read-fallback dla starszych instalacji laba; `init` automatycznie migruje go do kanonicznej ścieżki.

Parser musi być strict. Nieznany klucz, zły typ albo wartość spoza enumu kończy walidację exit code `2`. `init` tworzy plik tylko, gdy go nie ma. `init --force` może nadpisać konfigurację, ale musi najpierw zapisać backup `config/agentic-os.yml.bak.<timestamp>`.

Minimalny przykład:

```yaml
runtime:
  root: agentic-os-runtime
  timezone: Europe/Warsaw
  max_parallel_tasks: 4
  heartbeat_seconds: 20
  lease_ttl_seconds: 60
  stale_lease_seconds: 90
  shutdown_grace_seconds: 5
  timeouts:
    default_seconds: 1800
    docker_seconds: 240
    test_seconds: 3600
    model_seconds: 1800
    report_seconds: 300

sut:
  root: .
  compose_file: docker-compose.yml
  compose_project_name: agentic-os-sut
  autostart: true
  healthcheck:
    command: ["curl", "-fsS", "http://127.0.0.1:3000/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  install_shim_allowed: false

models:
  planner:
    provider: claude
    command: ["claude", "--model", "opus"]
    role: opus
  implementer:
    provider: claude
    command: ["claude", "--model", "sonnet"]
    role: sonnet
  reviewer:
    provider: codex
    command: ["codex"]
    role: codex

dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: false

paths:
  reports: reports
  bugs: bugs
  evidence: evidence
  prompts: config/prompts

reports:
  copy_reports_script: scripts/copy-reports.sh
  extract_last_run_script: scripts/extract-last-run.sh
  build_summary_script: scripts/build-summary.sh
  require_reports_on_failure: true

gates:
  known_bugs_fail_exit: true
  assertion_changes_require_decision: true
  exact_spec_failure_opens_bug: true
  require_functional_area_tag: true
  require_lifecycle_tag: true
  infrastructure_exit_code: 2
```

JSON Schema do skopiowania przez fazę 03:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Agentic OS configuration",
  "type": "object",
  "additionalProperties": false,
  "required": ["runtime", "sut", "models", "dashboard", "paths", "reports", "gates"],
  "properties": {
    "runtime": {
      "type": "object",
      "additionalProperties": false,
      "required": ["root", "timezone", "max_parallel_tasks", "heartbeat_seconds", "lease_ttl_seconds", "stale_lease_seconds", "shutdown_grace_seconds", "timeouts"],
      "properties": {
        "root": { "type": "string", "minLength": 1 },
        "timezone": { "type": "string", "minLength": 1 },
        "max_parallel_tasks": { "type": "integer", "minimum": 1, "maximum": 16 },
        "heartbeat_seconds": { "type": "integer", "minimum": 5 },
        "lease_ttl_seconds": { "type": "integer", "minimum": 10 },
        "stale_lease_seconds": { "type": "integer", "minimum": 10 },
        "shutdown_grace_seconds": { "type": "integer", "minimum": 1 },
        "timeouts": {
          "type": "object",
          "additionalProperties": false,
          "required": ["default_seconds", "docker_seconds", "test_seconds", "model_seconds", "report_seconds"],
          "properties": {
            "default_seconds": { "type": "integer", "minimum": 1 },
            "docker_seconds": { "type": "integer", "minimum": 1 },
            "test_seconds": { "type": "integer", "minimum": 1 },
            "model_seconds": { "type": "integer", "minimum": 1 },
            "report_seconds": { "type": "integer", "minimum": 1 }
          }
        }
      }
    },
    "sut": {
      "type": "object",
      "additionalProperties": false,
      "required": ["root", "compose_file", "compose_project_name", "autostart", "healthcheck", "test_runner", "install_shim_allowed"],
      "properties": {
        "root": { "type": "string", "minLength": 1 },
        "compose_file": { "type": ["string", "null"] },
        "compose_project_name": { "type": "string", "minLength": 1 },
        "autostart": { "type": "boolean" },
        "healthcheck": {
          "type": "object",
          "additionalProperties": false,
          "required": ["command", "timeout_seconds", "retries"],
          "properties": {
            "command": {
              "type": "array",
              "minItems": 1,
              "items": { "type": "string" }
            },
            "timeout_seconds": { "type": "integer", "minimum": 1 },
            "retries": { "type": "integer", "minimum": 0 }
          }
        },
        "test_runner": { "type": "string", "minLength": 1 },
        "install_shim_allowed": { "type": "boolean" }
      }
    },
    "models": {
      "type": "object",
      "additionalProperties": false,
      "required": ["planner", "implementer", "reviewer"],
      "properties": {
        "planner": { "$ref": "#/$defs/model" },
        "implementer": { "$ref": "#/$defs/model" },
        "reviewer": { "$ref": "#/$defs/model" }
      }
    },
    "dashboard": {
      "type": "object",
      "additionalProperties": false,
      "required": ["host", "port", "enable_write_endpoints"],
      "properties": {
        "host": { "const": "127.0.0.1" },
        "port": { "type": "integer", "minimum": 1024, "maximum": 65535 },
        "enable_write_endpoints": { "type": "boolean" }
      }
    },
    "paths": {
      "type": "object",
      "additionalProperties": false,
      "required": ["reports", "bugs", "evidence", "prompts"],
      "properties": {
        "reports": { "type": "string", "minLength": 1 },
        "bugs": { "type": "string", "minLength": 1 },
        "evidence": { "type": "string", "minLength": 1 },
        "prompts": { "type": "string", "minLength": 1 }
      }
    },
    "reports": {
      "type": "object",
      "additionalProperties": false,
      "required": ["copy_reports_script", "extract_last_run_script", "build_summary_script", "require_reports_on_failure"],
      "properties": {
        "copy_reports_script": { "type": "string", "minLength": 1 },
        "extract_last_run_script": { "type": "string", "minLength": 1 },
        "build_summary_script": { "type": "string", "minLength": 1 },
        "require_reports_on_failure": { "const": true }
      }
    },
    "gates": {
      "type": "object",
      "additionalProperties": false,
      "required": ["known_bugs_fail_exit", "assertion_changes_require_decision", "exact_spec_failure_opens_bug", "require_functional_area_tag", "require_lifecycle_tag", "infrastructure_exit_code"],
      "properties": {
        "known_bugs_fail_exit": { "const": true },
        "assertion_changes_require_decision": { "const": true },
        "exact_spec_failure_opens_bug": { "const": true },
        "require_functional_area_tag": { "const": true },
        "require_lifecycle_tag": { "const": true },
        "infrastructure_exit_code": { "const": 2 }
      }
    }
  },
  "$defs": {
    "model": {
      "type": "object",
      "additionalProperties": false,
      "required": ["provider", "command", "role"],
      "properties": {
        "provider": { "type": "string", "enum": ["claude", "codex", "script"] },
        "command": {
          "type": "array",
          "minItems": 1,
          "items": { "type": "string" }
        },
        "role": { "type": "string", "enum": ["opus", "sonnet", "codex", "script"] }
      }
    }
  }
}
```

## 4. Docker and SUT lifecycle

Agentic OS obsługuje Docker Compose jako opcjonalny lifecycle SUT. SUT startuje tylko, jeżeli `sut.compose_file` istnieje i `sut.autostart=true`.

Preflight:

1. `docker version` musi przejść przed każdą fazą wymagającą SUT.
2. `docker compose -f <compose_file> config` musi przejść przed `up`.
3. Brak Dockera, brak compose file przy `autostart=true` albo błąd compose config to infra fail, exit `2`.

Start:

1. utwórz task `kind='run'` z `payload.workflow='sut_start'`;
2. wykonaj `docker compose -p <compose_project_name> -f <compose_file> up -d`;
3. wykonuj healthcheck do limitu `healthcheck.retries`;
4. zapisz log subprocessu i eventy `sut.compose_up`, `sut.healthcheck_passed` albo `sut.healthcheck_failed`;
5. przy failu zapisz evidence i status `failure_kind='infra'`.

Stop:

1. `down` zatrzymuje procesy Agentic OS;
2. Compose SUT jest zatrzymywany tylko, jeżeli run wystartował go w tej sesji albo operator poda `--stop-sut`;
3. `docker compose down` jest logowany jako osobny run i nigdy nie usuwa wolumenów bez jawnego `--volumes`.

Recovery:

- po crashu `resume` nie zakłada, że Compose jest w dobrym stanie;
- najpierw sprawdza healthcheck;
- jeżeli kontenery działają i healthcheck przechodzi, kontynuuje;
- jeżeli healthcheck nie przechodzi, runtime uruchamia `docker compose up -d` jeszcze raz;
- drugi fail oznacza task jako `failed`, `failure_kind='infra'`, exit `2`.

## 5. Subprocess and log contract

Każde wykonanie zewnętrzne musi przejść przez wrapper `agentic_os.runtime.subprocess.run`.

Wymagania:

- argumenty jako lista, bez `shell=True`, chyba że wywoływany jest zaufany skrypt z tego repo;
- `cwd` jawne i zapisane w tabeli `runs`;
- env filtrowane i hashowane do `env_hash`; sekrety nie trafiają do DB ani logu;
- `start_new_session=True`, kill process group po timeoucie;
- `SIGTERM`, potem po `runtime.shutdown_grace_seconds` `SIGKILL`;
- stdout i stderr zapisane razem do `agentic-os-runtime/logs/subprocess/<run-id>.log`;
- tail ostatnich linii może trafić do eventów, ale pełny log jest tylko w pliku;
- koniec subprocessu zawsze wywołuje finalizer evidence przed aktualizacją `runs.finished_at`.

Manifest wyniku w `agentic-os-runtime/evidence/<run-id>/manifest.json`:

```json
{
  "schema_version": 1,
  "run_id": "01H...",
  "task_id": "01H...",
  "phase_id": "02-codex-runtime-contract",
  "kind": "run-tests",
  "command": ["./run-tests.sh"],
  "cwd": "/absolute/path/to/sut",
  "started_at": "2026-05-16T19:00:00Z",
  "finished_at": "2026-05-16T19:03:31Z",
  "exit_code": 1,
  "failure_kind": "product",
  "sut": {
    "git_sha": "unknown",
    "compose_project": "agentic-os-sut",
    "docker_images": []
  },
  "artifacts": [
    {
      "path": "reports/summary.md",
      "sha256": "..."
    }
  ]
}
```

Manifest musi powstać dla tasków `kind in ('run','bug','recovery')` oraz workflowów `dry-run`, `run-tests`, `sut_start` i `sut_stop`. Brak manifestu po subprocessie jest P1 i finalizer ustawia `failure_kind='unknown'`.

## 6. `run-tests.sh` exit contract

`run-tests.sh` jest pojedynczym runnerem testów QualityCat. Agentic OS nie zmienia jego kodu wyjścia.

Znaczenie kodów:

| Kod | Znaczenie | Klasyfikacja w DB |
|---:|---|---|
| 0 | Wszystkie wymagane testy przeszły, raporty istnieją. | `failure_kind=NULL`, task `succeeded` |
| 1 | Fail produktowy albo nadal czerwony `@known-bug`. | `failure_kind='product'`, task `failed` |
| 2 | Błąd infrastruktury, konfiguracji, SUT, Gradle compile, DB, Docker albo raportowania. | `failure_kind='infra'`, task `failed` |
| 130 | Przerwanie przez operatora. | `failure_kind='user_abort'`, task `cancelled` |
| inny | Nieznany kod. | `failure_kind='unknown'`, task `failed`, `unmapped_exit=true` |

Runner ma stałą kolejność:

1. start lub healthcheck SUT, jeżeli Compose jest dostępny;
2. assertion guard;
3. Gradle/Cucumber;
4. generacja Cucumber HTML, JUnit XML, Allure static i `reports/summary.md`;
5. `extract-last-run.sh`;
6. `build-summary.sh`, jeżeli istnieje;
7. zwrot oryginalnego produktu testów jako `0` albo `1`, chyba że wystąpił infra fail.

Raporty i evidence muszą powstać przed zwrotem `1`. `@known-bug` nigdy nie jest tłumaczony na green. Cichy green dla znanego buga jest naruszeniem kontraktu P0.

## 7. Recovery behavior

Start runtime zawsze wykonuje lekki recovery scan:

1. `PRAGMA integrity_check` na SQLite;
2. odczyt lease z DB i `agentic-os-runtime/leases/`;
3. ping PID z `agentic-os-runtime/pids/`;
4. oznaczenie porzuconych tasków `failed` z `error_class='abandoned'`;
5. zapis eventów `recovery.scan_started`, `recovery.lease_expired`, `recovery.applied`;
6. odświeżenie `event_offsets`, jeżeli pliki NDJSON były przesunięte.

`resume` jest operatorem jawnym: wykonuje recovery scan, podnosi orchestrator i przechodzi do pierwszego taska `queued` albo do taska `failed/abandoned`, który ma `payload.resume_allowed=true`. `resume` nie retry'uje automatycznie komendy, która miała product fail `1`.

## 8. Handoff to phase 03

Faza 03 implementuje minimum:

- `scripts/agentic-os.sh`;
- `scripts/agentic-os/agentic_os/__init__.py`;
- `scripts/agentic-os/agentic_os/cli.py`;
- `scripts/agentic-os/agentic_os/config.py`;
- `scripts/agentic-os/agentic_os/storage/schema.sql`;
- `scripts/agentic-os/agentic_os/storage/db.py`;
- `scripts/agentic-os/agentic_os/orchestrator.py`;
- minimalny `dry-run`;
- `config/agentic-os.yml.example`.

Faza 03 nie zmienia semantyki exit code, nazw tabel ani znaczenia aliasów CLI. Jeżeli implementacja potrzebuje uproszczenia, musi zostawić pole lub komendę jako stub z jawnym eventem `capability.not_implemented`, a nie zmieniać kontraktu.
