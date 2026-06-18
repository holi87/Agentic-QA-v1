# Agentic OS runtime contract

Status: active

- Contract gate: Accepted
- Depends on: `docs/cli-contract.md`, `docs/database-schema.md`

This document is the execution contract of the runtime. If this document and `docs/cli-contract.md` conflict, the CLI contract wins for command names and exit codes.

## 1. Runtime boundary

Agentic OS is a local Python 3.13+ runtime started via the thin shim `scripts/agentic-os.sh`. The shim contains no workflow logic, only:

1. determines the repo directory;
2. sets `PYTHONPATH=scripts/agentic-os`;
3. executes `python -m agentic_os <args>`;
4. propagates the Python process exit code without translation.

The runtime writes only to:

- `agentic-os-runtime/` as private operational state;
- `reports/`, `bugs/`, `evidence/` as artifacts delivered to the operator;
- `config/agentic-os.yml` if the operator invoked `init` and the file does not exist (`init` also migrates a legacy `.qualitycat/agentic-os.yml` into the canonical path);
- `config/agentic-os.yml.example`, templates, and framework repo code during framework implementation.

The runtime does not write to the SUT. There are exactly two exceptions: `sandbox-sut/` in phase 09, and the installation of the shim `run-tests.sh` in the SUT after an explicit `--install-shim`.

## 2. Directory contract

Canonical runtime layout:

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

Public artifacts:

```text
reports/
bugs/
evidence/
```

`agentic-os-runtime/evidence/<run-id>/manifest.json` is the source of truth for a run's files. The root `evidence/` is the handoff copy for the operator and QualityCat tooling.

## 3. Configuration contract

Configuration file: `config/agentic-os.yml` (canonical). `.qualitycat/agentic-os.yml` is accepted as a read fallback for upgrading lab installs; `init` auto-migrates it into the canonical path.

The parser must be strict. An unknown key, wrong type, or value outside the enum ends validation with exit code `2`. `init` creates the file only if it does not exist. `init --force` may overwrite the configuration, but must first save a backup `config/agentic-os.yml.bak.<timestamp>`.

Minimal example:

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

JSON Schema to be copied by phase 03:

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

Agentic OS handles Docker Compose as an optional SUT lifecycle. The SUT starts only if `sut.compose_file` exists and `sut.autostart=true`.

Preflight:

1. `docker version` must pass before any phase that requires the SUT.
2. `docker compose -f <compose_file> config` must pass before `up`.
3. Missing Docker, missing compose file with `autostart=true`, or a compose config error is an infra fail, exit `2`.

Start:

1. create a task `kind='run'` with `payload.workflow='sut_start'`;
2. execute `docker compose -p <compose_project_name> -f <compose_file> up -d`;
3. run healthcheck up to the `healthcheck.retries` limit;
4. write the subprocess log and the events `sut.compose_up`, `sut.healthcheck_passed` or `sut.healthcheck_failed`;
5. on failure write evidence and status `failure_kind='infra'`.

Stop:

1. `down` stops the Agentic OS processes;
2. the Compose SUT is stopped only if a run started it in this session or the operator passes `--stop-sut`;
3. `docker compose down` is logged as a separate run and never removes volumes without an explicit `--volumes`.

Recovery:

- after a crash, `resume` does not assume Compose is in a good state;
- it checks the healthcheck first;
- if the containers run and the healthcheck passes, it continues;
- if the healthcheck does not pass, the runtime runs `docker compose up -d` once more;
- a second failure marks the task as `failed`, `failure_kind='infra'`, exit `2`.

## 5. Subprocess and log contract

Every external execution must go through the wrapper `agentic_os.runtime.subprocess.run`.

Requirements:

- arguments as a list, no `shell=True`, unless the trusted script being called comes from this repo;
- explicit `cwd` saved in the `runs` table;
- env filtered and hashed into `env_hash`; secrets do not reach the DB or log;
- `start_new_session=True`, kill the process group after a timeout;
- `SIGTERM`, then after `runtime.shutdown_grace_seconds` `SIGKILL`;
- stdout and stderr saved together to `agentic-os-runtime/logs/subprocess/<run-id>.log`;
- the tail of the last lines may go into events, but the full log lives only in the file;
- the end of a subprocess always invokes the evidence finalizer before updating `runs.finished_at`.

Result manifest in `agentic-os-runtime/evidence/<run-id>/manifest.json`:

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

The manifest must be produced for tasks `kind in ('run','bug','recovery')` and for the workflows `dry-run`, `run-tests`, `sut_start`, and `sut_stop`. A missing manifest after a subprocess is a P1 and the finalizer sets `failure_kind='unknown'`.

## 6. `run-tests.sh` exit contract

`run-tests.sh` is the single QualityCat test runner. Agentic OS does not change its exit code.

Code meaning:

| Code | Meaning | DB classification |
|---:|---|---|
| 0 | All required tests passed, reports exist. | `failure_kind=NULL`, task `succeeded` |
| 1 | Product fail or a still-red `@known-bug`. | `failure_kind='product'`, task `failed` |
| 2 | Infrastructure, configuration, SUT, Gradle compile, DB, Docker, or reporting error. | `failure_kind='infra'`, task `failed` |
| 130 | Operator-initiated interruption. | `failure_kind='user_abort'`, task `cancelled` |
| other | Unknown code. | `failure_kind='unknown'`, task `failed`, `unmapped_exit=true` |

The runner has a fixed order:

1. start or healthcheck the SUT, if Compose is available;
2. assertion guard;
3. Gradle/Cucumber;
4. generate Cucumber HTML, JUnit XML, Allure static, and `reports/summary.md`;
5. `extract-last-run.sh`;
6. `build-summary.sh`, if it exists;
7. return the original test product as `0` or `1`, unless an infra fail occurred.

Reports and evidence must be produced before a `1` is returned. `@known-bug` is never translated to green. Silent green for a known bug is a P0 contract violation.

## 7. Recovery behavior

Runtime start always performs a light recovery scan:

1. `PRAGMA integrity_check` on SQLite;
2. read leases from the DB and `agentic-os-runtime/leases/`;
3. ping PIDs from `agentic-os-runtime/pids/`;
4. mark abandoned tasks as `failed` with `error_class='abandoned'`;
5. write events `recovery.scan_started`, `recovery.lease_expired`, `recovery.applied`;
6. refresh `event_offsets` if NDJSON files have been rolled over.

`resume` is explicit operator action: it runs the recovery scan, brings up the orchestrator, and moves to the first `queued` task or to a `failed/abandoned` task that has `payload.resume_allowed=true`. `resume` does not automatically retry a command that had a product fail `1`.

## 8. Handoff to phase 03

Phase 03 implements the minimum:

- `scripts/agentic-os.sh`;
- `scripts/agentic-os/agentic_os/__init__.py`;
- `scripts/agentic-os/agentic_os/cli.py`;
- `scripts/agentic-os/agentic_os/config.py`;
- `scripts/agentic-os/agentic_os/storage/schema.sql`;
- `scripts/agentic-os/agentic_os/storage/db.py`;
- `scripts/agentic-os/agentic_os/orchestrator.py`;
- a minimal `dry-run`;
- `config/agentic-os.yml.example`.

Phase 03 does not change exit code semantics, table names, or the meaning of CLI aliases. If the implementation needs simplification, it must leave the field or command as a stub with an explicit `capability.not_implemented` event, not modify the contract.
