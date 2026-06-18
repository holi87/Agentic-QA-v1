# Agentic OS CLI contract

Status: active

- Contract gate: Accepted for implementation
- Phase: `phase/02-codex-runtime-contract`
- Entrypoint: `scripts/agentic-os.sh`

The CLI is the operator's public API. The commands from ADR 0001 are canonical. The names from the earlier plan (`serve`, `start`, `resume`, `dry-run`) are compatibility aliases and must remain, because the checklist and later phases rely on them.

## 1. Invocation

```bash
scripts/agentic-os.sh <command> [options]
```

Shim requirements:

- works from any directory inside the repo;
- detects the repo root via `git rev-parse --show-toplevel`, and when git is not available, via the shim's own location;
- sets `PYTHONPATH=<repo>/scripts/agentic-os`;
- executes `python3 -m agentic_os <command> [options]`;
- propagates the Python exit code;
- does not run workflow logic in bash.

Global options:

| Option | Meaning |
|---|---|
| `--config <path>` | Defaults to `config/agentic-os.yml`; legacy `.qualitycat/agentic-os.yml` is fallback only. |
| `--root <path>` | Repo/SUT root for the operation, defaults to the current repo. |
| `--json` | Machine-readable JSON output on stdout. |
| `--verbose` | Larger log tail on stderr. |
| `--no-color` | No colors in the output. |

## 2. Exit codes

| Code | Meaning |
|---:|---|
| `0` | Command completed successfully. For tests this means green. |
| `1` | Product/test fail, including a red `@known-bug`. |
| `2` | Infra/runtime/config fail. |
| `64` | CLI usage error: unknown command, missing argument, invalid option. |
| `130` | Operator-initiated interruption. |

A CLI command must not turn a `run-tests.sh` exit `1` into `0`. `status` may return `0` even if the last run had a product fail, because reading the status itself succeeded.

`doctor --autonomy` and `autonomy bootstrap` use an extended contract (issue #266):

| Code | Meaning |
|---:|---|
| `0` | Autonomy ready. |
| `2` | Config bad (autonomy cannot load `config/agentic-os.yml`). |
| `3` | Provider unavailable (a `models.<role>` provider/fallback fails its smoke). |
| `4` | Budget misconfigured. |

## 3. Canonical commands

### `init`

```bash
scripts/agentic-os.sh init [--force] [--install-shim]
```

Creates `agentic-os-runtime/`, initializes SQLite, creates `config/agentic-os.yml` if it does not exist, and validates the public artifact directories.

Rules:

- without `--force` does not overwrite the config;
- `--force` saves a backup of the config;
- `--install-shim` may write `run-tests.sh` in the SUT only if `sut.install_shim_allowed=true`;
- writes the events `runtime.initialized`, `config.created`, `db.migration_applied`.

### `doctor`

```bash
scripts/agentic-os.sh doctor [--sut] [--models] [--docker]
```

Checks Python, SQLite, config, write permissions, free space, Docker, Compose, JVM/Gradle, the dashboard port, and the local model CLIs.

Exit:

- `0` if the selected checks passed;
- `2` if any required check failed;
- `64` if the options are invalid.

### `up`

```bash
scripts/agentic-os.sh up [--foreground] [--dashboard-only] [--stop-existing]
```

Starts the orchestrator and dashboard on `127.0.0.1:8765`. Runs in the background by default. `--foreground` keeps the process in the terminal. `--dashboard-only` runs only the FastAPI/HTMX read UI and does not lease tasks.

Rules:

- takes the `orchestrator` lease before starting;
- on lease conflict returns `2` and prints owner/PID;
- runs a recovery scan;
- if `sut.autostart=true`, runs the Docker/SUT lifecycle;
- writes the PID to `agentic-os-runtime/pids/`.

### `down`

```bash
scripts/agentic-os.sh down [--stop-sut] [--volumes]
```

Stops the Agentic OS processes, flushes events, and releases leases.

Rules:

- `--stop-sut` runs Compose down only for the project from the config;
- `--volumes` requires `--stop-sut` and records an operator decision;
- absence of a running process is not an error.

### `run`

```bash
scripts/agentic-os.sh run <workflow> [--phase <phase-id>] [--tag <expr>] [--dry] [--retry-of <task-id>]
```

Phase 03 workflows:

| Workflow | Meaning |
|---|---|
| `dry-run` | Minimal task without a real SUT; creates DB, event, run, and manifest. |
| `run-tests` | Runs `sut.test_runner` preserving the exit contract. |
| `recovery` | Runs the recovery scan and reports actions. |

Later-phase workflows may add `bug-adjudicate`, `qualitycat-sync`, `final-gate`, but must preserve the `tasks/runs/events` tables.

### `task`

```bash
scripts/agentic-os.sh task create <task-spec.md>
scripts/agentic-os.sh task list
scripts/agentic-os.sh task show <task-id>
scripts/agentic-os.sh task analyze <task-id>
scripts/agentic-os.sh task plan <task-id>
scripts/agentic-os.sh task candidates <task-id>
scripts/agentic-os.sh task approve-candidate <task-id> <candidate-id> [--expected-assertion <text>] [--test-data <text>] [--cleanup-strategy <text>] [--target-page <path>]
scripts/agentic-os.sh task reject-candidate <task-id> <candidate-id> --reason <text>
scripts/agentic-os.sh task mark-needs-decision <task-id> <candidate-id> --reason <text>
scripts/agentic-os.sh task implement-tests <task-id>
scripts/agentic-os.sh task abandon-patch <task-id> --patch <rel-path> --reason <text>
```

Operator-level workflow for test tasks visible in the dashboard.

Rules:

- `create` accepts a Markdown task description, copies it to
  `agentic-os-runtime/task-specs/` and creates a `work_items` row;
- the ID has the format `TASK-YYYYMMDD-HHMMSS-<slug>`;
- a new task gets `status='queued'`, a stable `spec_path`, `sut_root`, and
  `priority`;
- `list` returns the work item queue from newest to oldest;
- `show` returns the task and its registered artifacts;
- `analyze` writes `sut-map.json`, `requirements.md`, `risk-map.md`,
  `candidate-tests.md`, and `candidate-tests.json` into
  `agentic-os-runtime/analysis/<task-id>/` and sets status `analyzing`;
- `plan` produces `TEST-PLAN.md` and `TEST-PLAN.json` in
  `agentic-os-runtime/plans/<task-id>/` and sets status `planned` (requires a prior
  `analyze`);
- `candidates` lists the structured `TEST-PLAN.json` items and decision
  counters;
- `approve-candidate` promotes one item to `generate_now`. It refuses approval
  when `validate_plan()` finds blocking issues such as a missing HTTP status,
  missing UI assertion target, or missing cleanup for mutating API methods;
- `reject-candidate` sets `decision='not_testable'` and records the reason in
  plan notes;
- `mark-needs-decision` moves an item back to operator review;
- `abandon-patch` writes an artifact with `verdict: ABANDONED`, inserts a row
  in `decisions` (operator, topic `patch_abandoned:<path>`), preserves the
  patch history, and unblocks the final gate. Requires `--patch` (a path
  relative to the repo) and `--reason` (non-empty text).
  `find_patch_gate_violations` treats a patch with an `APPROVE` or
  `ABANDONED` artifact as resolved;
- `implement-tests` always generates a reviewable skeleton patch in
  `agentic-os-runtime/patches/<task-id>/<hash>.patch` and registers
  `work_item_artifacts.kind='patch'`. If no candidate is approved for
  executable generation it sets status `blocked` and returns
  `needs_operator_decision`. If one or more candidates are approved, it also
  emits Playwright TS files under a v2 patch bundle and sets status
  `implementing`. The patch is not applied; application goes only through
  `run review-gate --apply-patch ... --scope api|ui|assertion` with an
  APPROVE verdict;
- the runtime does not write to the SUT during task creation.

### `inbox`

```bash
scripts/agentic-os.sh inbox list
scripts/agentic-os.sh inbox ingest
scripts/agentic-os.sh inbox synthesize [--title <task-title>]
```

Task-document intake visible in both CLI and dashboard.

Rules:

- `list` returns pending files from `./inbox/` and `./pretask/`, excluding
  hidden files plus `.archive/` and `.failed/`;
- `ingest` parses each pending `.md`, `.markdown`, `.txt`, `.docx`, or `.pdf`
  file into a separate task spec under `agentic-os-runtime/task-specs/`;
- `synthesize` parses all pending files into one combined task spec with source
  references, extracted requirements, endpoints/pages, known-bug hints,
  test-data constraints, and open questions;
- successful sources move to `<intake>/.archive/<stem>-<UTC-ts>.<ext>`;
- failed sources move to `<intake>/.failed/` with a sidecar
  `<name>.error.txt`;
- `.docx` requires `python-docx`; `.pdf` requires `pypdf`; parser failure for
  one file must not abort the rest of the batch.

### `status`

```bash
scripts/agentic-os.sh status [--watch] [--json] [--phase <phase-id>]
```

Shows:

- active leases;
- phases and their statuses;
- task counts by status;
- recent runs with exit codes;
- open blockers;
- bug counts by severity/status;
- the location of the latest manifest.

`--json` returns the object:

```json
{
  "runtime": "ready|degraded|blocked",
  "db": "ok|missing|corrupt",
  "leases": [],
  "phases": [],
  "tasks": { "queued": 0, "running": 0, "failed": 0 },
  "bugs": { "open": 0, "known": 0 },
  "last_run": null
}
```

### `logs`

```bash
scripts/agentic-os.sh logs [--run <run-id>] [--phase <phase-id>] [--follow] [--lines <n>]
```

Tails `agentic-os-runtime/events/current` or the subprocess log of a given run. `--follow` behaves like `tail -f`. A missing log for an existing run is an infra fail `2`.

### `support-bundle`

```bash
scripts/agentic-os.sh support-bundle [--dest <path>] [--include <list>] [--exclude <list>] [--no-redact] [--tag <name>]
```

Builds a redacted diagnostic tarball under `agentic-os-runtime/support-bundles/` (or `--dest`). Subsystems are: `config`, `doctor`, `events`, `runs`, `bugs`. By default all are included.

| Flag | Meaning |
|---|---|
| `--dest <path>` | Write the tarball to this directory instead of the runtime default. The directory is created if missing. |
| `--include <list>` | Comma-separated subsystem names to gather. Mutually exclusive with `--exclude`. |
| `--exclude <list>` | Comma-separated subsystem names to drop from the default set. Mutually exclusive with `--include`. |
| `--no-redact` | Embed `config/agentic-os.yml` verbatim instead of applying the secret-key denylist. The manifest's `redacted: false` makes the choice auditable; only use when the operator owns the bundle's destination. |
| `--tag <name>` | Appends `-<name>` to the bundle filename (alphanumerics, dot, dash, underscore). |

Unknown subsystem names and combining `--include` with `--exclude` return `64`.

### `autonomy`

```bash
scripts/agentic-os.sh autonomy <start|stop|pause|resume|status|preflight|follow|bootstrap> [--json]
```

Headless control of the in-process autonomy session (issues #244, #266).

| Subcommand | Meaning |
|---|---|
| `start [--max-minutes N]` | Start a session (refuses with `2` when preflight fails). |
| `stop` | Cooperatively stop the worker. |
| `pause` / `resume` | Park the worker between steps / continue it. State (DB connection, session identity) is retained across a pause. |
| `status` | Current session payload; `paused` is surfaced as its own status. |
| `preflight` | Readiness checklist. |
| `follow [--from <id>] [--filter k=v]` | Tail the NDJSON event stream. |
| `bootstrap [--max-minutes N] [--no-start]` | One-shot onboarding: `init` → `doctor --autonomy` (gate) → `git ensure` (when enabled) → start. Idempotent. `--no-start` stops after the readiness gate. Exit codes follow `doctor --autonomy` (0/2/3/4). |

### `verifications`

```bash
scripts/agentic-os.sh verifications <list|show|override> [DEC-ID] [--actor A] [--work-item W] [--limit N] [--severity Sx] [--reason R] [--json]
```

CLI access to the reviewer/triager decision trail (issue #266). `override`
writes an operator decision row and sets `reversed_by` on the target. A missing
decision id returns `4`.

### `budget`

```bash
scripts/agentic-os.sh budget <show|set|reset> [--session SID] [--role planner|implementer|reviewer|triager] [--max-tokens N] [--json]
```

Token/USD budget view + runtime overrides (issue #266). `show` reports session
and per-role consumption vs limits with percentages. `set` persists a limit to
`config/agentic-os.yml`. `reset --session SID` zeroes session counters by
dropping that session's `model_invocations` rows.

### `reports`

```bash
scripts/agentic-os.sh reports <list|show|diff|html> [name...] [--type T] [--output DIR] [--json]
```

Browse `reports/` artifacts from a headless box (issue #266). `list` is newest
first, optionally filtered by filename prefix `--type`. `show` prints one
report; `diff` compares the numeric fields of two JSON report manifests. A name
outside `reports/` or a missing report returns `4`.

`html` (issue #372) (re)generates the human `how-to-run.html` guide from
`templates/how-to-run.html.template` into `--output DIR` (default
`<repo>/output`), filling values (base URLs, token env name) from the project
config when present. Standalone — no run or DB needed — and idempotent
(re-running rewrites identical bytes).

### `notifications`

```bash
scripts/agentic-os.sh notifications test --channel <webhook|desktop|sound> [--json]
```

Fires a synthetic notification through one configured channel so the operator
can validate setup without waiting for a real block (issue #268). Exit `0` on
delivery, `1` on dispatch failure, `64` when the channel is not configured.

### `transcripts`

```bash
scripts/agentic-os.sh transcripts show <invocation-id> [--json]
```

Prints a model invocation's structured reasoning transcript — thinking, tool
calls, tool results and text — for headless inspection (issue #270). A missing
transcript returns `4`.

Every subcommand of `autonomy`, `verifications`, `budget`, `reports`,
`notifications` and `transcripts` accepts `--json` for a machine-readable
payload.

## 4. Compatibility aliases

An alias must appear in `--help` as an alias, not as separate semantics.

| Alias | Canonical mapping | Reason |
|---|---|---|
| `serve` | `up --foreground --dashboard-only` | Checklist and phase 05 expect the local dashboard via `serve`. |
| `start` | `up` | The source plan used `start` for the runtime. |
| `resume` | `up --foreground` after a forced `run recovery` | Recovery after an interrupted process from the source plan. |
| `dry-run` | `run dry-run` | Phases 03/04/05/08 and the final fake SUT proof use this name without `run`. |

`resume` performs:

1. config validation;
2. recovery scan;
3. start the orchestrator in foreground;
4. resume only tasks with `payload.resume_allowed=true` or `queued` tasks.

`dry-run --fake-sut` is allowed only in the final fake SUT proof phase
(phase 15). Earlier the option may return `64` with the message
`fake SUT is not implemented before final fake SUT proof`.

## 5. Output contract

Human output:

- the first line states which command and config are in use;
- errors go to stderr;
- `run` success shows the run id and manifest path;
- a product fail `1` shows that reports were generated or why evidence is missing;
- an infra fail `2` shows the error class and the closest log.

JSON output for `run`:

```json
{
  "ok": false,
  "exit_code": 1,
  "failure_kind": "product",
  "task_id": "01H...",
  "run_id": "run-01H...",
  "manifest_path": "agentic-os-runtime/evidence/run-01H.../manifest.json",
  "reports_path": "reports",
  "bugs_opened": []
}
```

JSON output for an infra fail must have `failure_kind='infra'` and `exit_code=2`.

## 6. Error handling requirements

Unknown command:

```text
error: unknown command '<name>'
try: scripts/agentic-os.sh --help
```

Invalid config:

```text
error: invalid config .qualitycat/agentic-os.yml
path: gates.known_bugs_fail_exit
expected: true
actual: false
```

Lease conflict:

```text
error: orchestrator lease is held
owner: orchestrator
pid: 12345
acquired_at: 2026-05-16T19:02:00Z
hint: scripts/agentic-os.sh status
```

Known bug red:

```text
run-tests failed with product failures (exit 1)
known bugs remain red by policy; this is not an infra failure
reports: reports/
manifest: agentic-os-runtime/evidence/<run-id>/manifest.json
```

## 7. Help text requirements

`scripts/agentic-os.sh --help` must list the canonical commands and aliases:

```text
Commands:
  init
  doctor
  up
  down
  run
  task
  status
  logs
  support-bundle

Compatibility aliases:
  serve    -> up --foreground --dashboard-only
  start    -> up
  resume   -> run recovery; up --foreground
  dry-run  -> run dry-run
```

Help must not suggest that `@known-bug` is greened or excluded by default.

## 8. Validation commands for implementers

The minimal set after phase 03 implementation:

```bash
bash -n scripts/agentic-os.sh
python -m py_compile scripts/agentic-os/agentic_os/*.py
scripts/agentic-os.sh init
scripts/agentic-os.sh dry-run
scripts/agentic-os.sh status --json
scripts/agentic-os.sh logs --lines 20
```

Phase 05 also:

```bash
scripts/agentic-os.sh serve
```

and a manual check of `http://127.0.0.1:8765`.

Phase 10 also:

```bash
scripts/agentic-os.sh task create path/to/spec.md
scripts/agentic-os.sh task list
scripts/agentic-os.sh task show <task-id>
```

A manual dashboard smoke should confirm `GET /api/tasks`,
`POST /api/tasks` blocked while `dashboard.enable_write_endpoints=false`, and
task creation after setting `enable_write_endpoints=true`.
