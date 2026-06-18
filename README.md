# Quality Cat Agentic Web Testing

> Autonomous QA for web apps and APIs. The operator points at a SUT (System
> Under Test) via YAML or dashboard; the OS analyzes OpenAPI/docs, generates
> executable Playwright TS tests, runs them, classifies failures as product
> bug / known bug / infra / flaky / test bug, files Markdown bugs with
> evidence, and shows everything in a dashboard.

**Polish version**: [`README_pl.md`](README_pl.md)

---

## Table of contents

1. [What it is](#what-it-is)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [First run](#first-run)
5. [Quick start: from a doc to a tested feature](#quick-start-from-a-doc-to-a-tested-feature)
6. [Configuring a SUT](#configuring-a-sut)
7. [Dashboard](#dashboard)
8. [Full operator workflow](#full-operator-workflow)
9. [CLI reference](#cli-reference)
10. [AI models (Opus / Sonnet / Codex / Gemini)](#ai-models)
11. [Security and guardrails](#security-and-guardrails)
12. [Troubleshooting](#troubleshooting)
13. [Repo layout](#repo-layout)

---

## What it is

Quality Cat Agentic Web Testing is a local QA automation framework. Two
names appear throughout these docs — **Agentic OS** is the orchestrator
shipped from this repo (CLI, dashboard, skills framework), and
**QualityCat** is the QA / test-execution domain it produces output
for (bug reports, test tags, generated Playwright + TypeScript tests).
See [`AGENTS.md`](AGENTS.md) glossary for the full split, and
`ADR-0002`
for the stack decision.

The operator provides:

- the application under test (SUT) — **external**: reached over web/API
  URL(s) plus an optional DB connection. The OS connects to it and never
  starts it. (Compose-managed local SUT autostart is legacy, pending
  removal in Wave 17 — see
  `ADR-0001`.)
- configuration: base URL, API base URL, paths to OpenAPI / docs,
  credentials references;
- a task spec (Markdown) describing what to test.

The OS then:

1. Analyzes the SUT (OpenAPI, docs, project layout).
2. Builds `TEST-PLAN.md` + `TEST-PLAN.json` with plan items (source ref,
   expected assertion, test data, cleanup strategy).
3. Generates executable Playwright TS tests (API + UI) as a patch artifact.
4. The operator approves the patch via review gate.
5. The OS runs tests, collects evidence (screenshots, traces, JUnit XML).
6. Classifies each failure and files `BUG-NNN-*.md` for exact-spec product
   bugs.
7. Final gate verifies policy compliance.

**What the OS will never do:**

- Modify the SUT (except `sandbox-sut/` lab mode).
- Apply a patch without an explicit `APPROVE` from the review gate.
- Weaken an assertion without an operator decision row in the DB.
- Greenwash a `@known-bug` (a known bug remaining red still exits 1).
- Log secrets (credentials are env/file references only).

---

## Requirements

| Component        | Version              | Required | Notes                                          |
|------------------|----------------------|----------|------------------------------------------------|
| Python           | 3.13                 | ✅       | Stdlib + PyYAML                                 |
| PyYAML           | ≥ 6.0                | ✅       | Only runtime dependency                         |
| SQLite           | bundled with Python  | ✅       | `state.db` with WAL                             |
| Docker + Compose | latest               | ⚪       | Only if `sut.autostart` is enabled              |
| Node.js + Playwright | LTS              | ⚪       | Only if you want to run generated tests locally |
| Model CLIs       | (opus/sonnet/codex/gemini) | ⚪ | Only if you use `models.*` invocation           |
| `gh` (GitHub CLI)| —                    | ⚪       | Only for HTTPS push fallback                    |

Platform: macOS / Linux. Shell: zsh or bash.

---

## Installation

```bash
# 1. Clone the repo
git clone git@github.com:holi87/agentic-web-testing.git agentic-os
cd agentic-os

# 2. Create venv + install runtime + dev (pytest) dependencies
python3.13 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"

# 3. Smoke-test the install
.venv/bin/python -m pytest tests/test_runtime_guards.py
```

### Run from Docker (no local Python needed)

Build the OS image and start the dashboard in one command — the SUT stays
external (ADR-0001), so nothing else is launched:

```bash
docker compose up
```

Open <http://127.0.0.1:8765/> and stop with `docker compose down`. Drop task
specs into `input/`, collect results from `output/`, and mount your own
config to override the baked default. Contracts:
[volumes](docs/docker-volume-contract.md) ·
[networking & secrets](docs/docker-networking-contract.md).

On **Linux**, make the output dirs writable by the container's uid first
(Docker Desktop on macOS/Windows maps this automatically):

```bash
mkdir -p output/reports output/bugs output/evidence
sudo chown -R 10001:10001 output input
```

If the last command reports `5 passed`, the environment is ready.

The repo ships three helper scripts:

```bash
./scripts/agentic-os.sh        # main CLI wrapper
./run-tests.sh                  # framework self-test (bug-aware policy)
./scripts/assertion-guard.py    # diff-time assertion guard
```

---

## First run

```bash
# 1. Initialize runtime + config
./scripts/agentic-os.sh init

# 2. Sanity-check the runtime layout (no SUT/Docker yet)
./scripts/agentic-os.sh --json doctor

# 3. Start the dashboard (foreground; Ctrl+C to stop)
./scripts/agentic-os.sh up --dashboard-only --foreground

# 4. Open in a browser
open http://127.0.0.1:8765
```

> The full `doctor --sut --docker --models` gate intentionally fails on a
> clean checkout: the default config points at `docker-compose.yml`, which
> is not in this repo. Run that flavour **after** configuring a SUT — see
> [Configuring a SUT](#configuring-a-sut) below.

`init` creates:

- `agentic-os-runtime/state.db` (WAL SQLite + events + leases);
- `config/agentic-os.yml` (copied from `.example`);
- commented STEP2 v2 sections (kind, base_url, openapi, docs, credentials,
  tests_dir, tests.api/ui.runner) ready to uncomment.

---

## Quick start: from a doc to a tested feature

The fastest path from a feature brief (Markdown, plain text, DOCX, or
extractable-text PDF) to a candidate test plan — five commands:

```bash
# 1. Initialize + dashboard with writes enabled (one-shot session)
./scripts/agentic-os.sh init
./scripts/agentic-os.sh up --dashboard-only --foreground --full   # leave this running

# 2. Drop a document into inbox/ (or pretask/ for multi-doc bundles)
cp your-task.md inbox/

# 3. Synthesize one task spec from everything pending
./scripts/agentic-os.sh inbox synthesize --title "Online blog regression"

# 4. Open the dashboard and review the candidate plan
open http://127.0.0.1:8765
```

What happens:

- `inbox/` is the canonical drop dir. `pretask/` is its visible alias for
  larger bundles (multiple notes, OpenAPI dumps, exploratory checklists).
- `inbox synthesize` reads every pending doc, extracts endpoints / pages /
  known bugs / data constraints, and writes ONE structured task spec.
  `inbox ingest` is the alternative one-task-per-document path.
- Successfully processed files move to `<intake>/.archive/<stem>-<UTC-ts>.<ext>`;
  failures land in `<intake>/.failed/` with a sidecar `*.error.txt`.
- PDF intake is **extractable-text-only** — scanned PDFs are quarantined
  with an OCR-not-supported hint (see issue #143). The dashboard inbox
  list shows an `extract: OK / LOW / FAILED` badge per file so you see
  the problem before clicking Ingest.

For the full pipeline and the dashboard variant (Upload + Ingest + Create
task from pending buttons on `/tasks/new`), see
[`docs/operator-guide.md` § "Ingesting external task documents"](docs/operator-guide.md#ingesting-external-task-documents).

---

## Configuring a SUT

### Mode 1: YAML (recommended for first run)

Edit `config/agentic-os.yml`. Minimal config:

```yaml
sut:
  root: .
  compose_file: docker-compose.yml
  compose_project_name: my-app
  autostart: true
  healthcheck:
    command: ["curl", "-fsS", "http://127.0.0.1:3000/health"]
    timeout_seconds: 30
    retries: 10
  test_runner: ./run-tests.sh
  install_shim_allowed: false
```

Full v2 config (optional, recommended for STEP2 flow):

```yaml
sut:
  root: .
  kind: web_api                    # web | api | web_api
  base_url: http://127.0.0.1:3000
  api_base_url: http://127.0.0.1:3000/api
  ui_url: http://127.0.0.1:3000
  compose_file: docker-compose.yml
  compose_project_name: my-app
  autostart: true
  healthcheck:
    command: ["curl", "-fsS", "http://127.0.0.1:3000/health"]
    timeout_seconds: 30
    retries: 10
  openapi:
    sources:
      - type: file                 # file | url
        value: docs/openapi.yaml
  docs:
    sources:
      - type: file
        value: docs/requirements.md
  credentials:
    ref_type: env                  # env | file | none
    value: TEST_USER_TOKEN         # env var name only, never the literal
  tests_dir: tests
  tests:
    api:
      runner: playwright-ts        # playwright-ts | pytest-httpx
    ui:
      runner: playwright-ts
  test_runner: ./run-tests.sh
  install_shim_allowed: false
```

**Validation rules:**

- URLs must be `http://` or `https://`. Other schemes → `ConfigError`.
- File paths may not contain `..` (path traversal).
- `credentials.value` with `ref_type: env` must be an env var name
  (alphanum + underscore, not starting with a digit).
- `dashboard.host` must stay `127.0.0.1` (changing it requires an operator
  decision).
- Unknown keys → error (unless on the optional whitelist).

### Verifying SUT, Docker and model wiring

Once a SUT is configured, run the full doctor gate:

```bash
./scripts/agentic-os.sh --json doctor --sut --docker --models
```

This gate is green only when **all** of the following hold:

- `sut.compose_file` exists on disk in `mode: local`, or is `null` with
  `mode: online`;
- `--docker` finds the `docker` CLI on `PATH` and the daemon is
  reachable;
- `--models` finds the configured planner/implementer/reviewer/triager
  CLI binaries on `PATH` (see `doctor_check_models` in
  `scripts/agentic-os/agentic_os/sut_lifecycle.py`).

Any missing dependency is blocking — fix the reported gap and re-run.

### Dashboard edits and write-enable mode

The dashboard starts in read-only mode by default. When writes are disabled,
editing panels, task action buttons, agent edits, connectivity tests, and
skill toggles are intentionally disabled; write endpoints return `403`.

For a temporary operator session, start the dashboard with an in-memory write
override:

```bash
./scripts/agentic-os.sh up --dashboard-only --foreground --full
```

Add `--no-autostart` if you only want to edit configuration and do not want
the dashboard to start the local Docker Compose SUT:

```bash
./scripts/agentic-os.sh up --dashboard-only --foreground --full --no-autostart
```

`--full` does not change YAML. After stopping and restarting without `--full`,
the dashboard returns to read-only unless the YAML setting below is enabled.

For persistent edits, enable write endpoints in `config/agentic-os.yml`:

```yaml
dashboard:
  host: 127.0.0.1
  port: 8765
  enable_write_endpoints: true
```

Restart the dashboard after changing YAML:

```bash
./scripts/agentic-os.sh down
./scripts/agentic-os.sh up --dashboard-only --foreground
```

Then use the dashboard UI, including `/agents`, or call write endpoints
directly, for example:

```bash
curl -X POST http://127.0.0.1:8765/api/config \
  -H 'Content-Type: application/json' \
  -d @new-config.json
```

Responses:

- `200 ok=true` — written to disk, validation passed.
- `400` — invalid config, host != 127.0.0.1, or missing required fields.
- `403` — `enable_write_endpoints=false`.

Quick check:

```bash
curl -fsS http://127.0.0.1:8765/api/config | jq '.dashboard'
```

`enable_write_endpoints: true` means edits are enabled for this session.
Keep `dashboard.host: 127.0.0.1`; the server is designed for local operator
use, not LAN exposure.

**GET /api/config** always masks `credentials.value`:

```json
{
  "sut": {
    "credentials": {"ref_type": "env", "value": "env:TEST_USER_TOKEN"}
  }
}
```

---

## Dashboard

Default URL `http://127.0.0.1:8765`. Sections:

| Section             | What it shows                                                            |
|---------------------|--------------------------------------------------------------------------|
| Full Autonomy       | One-click time-boxed autonomous run (analyze → plan → implement loop)    |
| SUT mode            | Toggle local (docker compose) ↔ online (existing URL); per-endpoint enable |
| Active task         | Current task + runtime stat tiles (queued/running/failed/blockers)       |
| SUT context         | Bento with 4 blocks: Core / URLs / Runners / Sources / Dashboard         |
| Agents              | Vertical per-role cards with provider/command editing, reload, and connectivity test |
| Skills              | Per-role enable/disable checkboxes for the loaded skills set             |
| Patch resolution    | Live chip counters + patches table with state (waiting/rejected/abandoned/approved) |
| Suggestions         | Heuristic next-step list (run_analyze / generate_tests / review_pending) |
| Active leases       | Which owner holds which SQLite lease                                     |
| Last run            | Last `run-tests` exit + reports                                          |
| Recent events       | SSE event stream (severity, payload)                                     |

### Full Autonomy

Pick a time budget (min 15, max 720 min — under 60 min the dashboard warns
that a full build + tests + reports cycle may not complete) and click
**Start full autonomy**. A daemon thread walks every pending work-item
through analyze → plan → implement-tests until the deadline or **Stop**
is clicked. Live timer + event log render in the panel. Some lifecycle
commands (e.g. installing system Docker, opening privileged ports) may
require sudo — in that case the dashboard must be restarted as root for
those steps to succeed. The OS still records what it managed to do.

### SUT mode

- **local** — `docker-compose up` lifecycle. `compose_file` required.
- **online** — already-running URL (no docker needed). Per-endpoint
  switches gate test generation: if `web.enabled=false` the implementer
  skips UI specs; if `api.enabled=false` it skips API specs.

Saving the panel writes through `/api/sut/mode` into
`config/agentic-os.yml`.

Task detail page (`/tasks/<id>`) adds:

- Timeline (Analyze → Plan → Implement → Review gate → Run tests → Final gate)
- Action buttons (disabled when `enable_write_endpoints=false`)
- Blocking patches table with an **Abandon** button (modal with reason input)
- Meta, spec, artifacts, events stream

**Visual polish (Subphase 10)**: animated aurora background, color-coded
chips (amber waiting, red rejected with pulse, violet abandoned, green
approved with glow), bento grid, magnetic hover, light/dark mode.

---

## Full operator workflow

```bash
# 1. Create a task from a Markdown spec
./scripts/agentic-os.sh task create path/to/spec.md

# 2. Grab the id from output (TASK-YYYYMMDD-HHMMSS-<slug>)
TASK_ID=TASK-20260519-203000-orders-negative

# 3. Analyze — reads OpenAPI + docs + scans SUT
./scripts/agentic-os.sh task analyze "$TASK_ID"

# 4. Plan — produces TEST-PLAN.md
./scripts/agentic-os.sh task plan "$TASK_ID"

# 5. Review candidates, then generate executable test patch files
./scripts/agentic-os.sh task candidates "$TASK_ID"
# approve desired API/UI candidates with task approve-candidate ...
./scripts/agentic-os.sh task implement-tests "$TASK_ID"

# 6. Review gate — approves the patch but does not apply it
./scripts/agentic-os.sh run review-gate \
  --scope api \
  --diff agentic-os-runtime/patches/$TASK_ID/<patch>.patch \
  --work-item "$TASK_ID"

# 7. Apply the approved patch
./scripts/agentic-os.sh run review-gate \
  --scope api \
  --diff agentic-os-runtime/patches/$TASK_ID/<patch>.patch \
  --apply-patch agentic-os-runtime/patches/$TASK_ID/<patch>.patch \
  --work-item "$TASK_ID"

# 8. Local SUT (if Compose is configured)
./scripts/agentic-os.sh run sut-start
./scripts/agentic-os.sh run sut-healthcheck

# 9. Run tests
./scripts/agentic-os.sh run run-tests --work-item "$TASK_ID"

# 9. Final gate (verifies patches approved, bug policy honored)
./scripts/agentic-os.sh run final-gate

# 10. Cleanup
./scripts/agentic-os.sh run sut-stop
```

### Abandon a stale patch

```bash
./scripts/agentic-os.sh task abandon-patch "$TASK_ID" \
  --patch agentic-os-runtime/patches/$TASK_ID/<run>/files/<spec>.diff \
  --reason "rejected after operator review; tracked in BUG-007"
```

The patch row stays in history, the decision is logged in `decisions`, and
the final gate skips this patch.

### Recovery after a crash

```bash
./scripts/agentic-os.sh --json run recovery
sqlite3 agentic-os-runtime/state.db "pragma foreign_key_check;"
```

### Legacy `.agentic-os/` runtime

Older checkouts kept runtime state in a hidden `.agentic-os/` directory.
The canonical location is now the visible `agentic-os-runtime/`. If
`agentic-os doctor` warns that both roots exist, consolidate with
`migrate-runtime`:

```bash
./scripts/agentic-os.sh migrate-runtime --dry-run   # report the plan
./scripts/agentic-os.sh migrate-runtime             # do it

# verify, then delete the archive:
ls .agentic-os.legacy-*/
rm -rf .agentic-os.legacy-*
```

When both runtimes already have a `state.db`, the migrator refuses (no
safe automatic merge). Pick the source of truth, move the other aside
manually, then re-run. `--force` is available but archives the
existing visible state under `agentic-os-runtime.clobbered-*/` so you
can still recover it.

If an operator intentionally sets `runtime.root: .agentic-os` in
`config/agentic-os.yml`, `doctor` does NOT warn — `.agentic-os/` is
then the official runtime, not a legacy artifact.

---

## CLI reference

Full contract: [`docs/cli-contract.md`](docs/cli-contract.md).

```bash
./scripts/agentic-os.sh init [--force] [--install-shim [--shim-dir DIR]] [--sample-sut]
./scripts/agentic-os.sh doctor [--sut] [--docker] [--models]
./scripts/agentic-os.sh up [--dashboard-only] [--foreground | --daemon] [--host H] [--port P] [--full] [--no-autostart] [--auto-repair] [--autonomy-minutes N]
./scripts/agentic-os.sh down [--timeout SECONDS]
./scripts/agentic-os.sh status
./scripts/agentic-os.sh logs [--follow] [--lines N] [--file PATH]
./scripts/agentic-os.sh crawler <start-url> [--depth N] [--max-pages M] [--browser]
./scripts/agentic-os.sh migrate-runtime [--dry-run] [--force]
./scripts/agentic-os.sh support-bundle
./scripts/agentic-os.sh inbox [list | ingest | synthesize]

./scripts/agentic-os.sh task create <spec.md>
./scripts/agentic-os.sh task list
./scripts/agentic-os.sh task show <task-id>
./scripts/agentic-os.sh task analyze <task-id>
./scripts/agentic-os.sh task plan <task-id>
./scripts/agentic-os.sh task implement-tests <task-id>
./scripts/agentic-os.sh task abandon-patch <task-id> --patch <p> --reason <r>

./scripts/agentic-os.sh run dry-run [--fake-sut]
./scripts/agentic-os.sh run recovery
./scripts/agentic-os.sh run sut-start
./scripts/agentic-os.sh run sut-healthcheck
./scripts/agentic-os.sh run sut-stop
./scripts/agentic-os.sh run run-tests [--work-item <id>]
./scripts/agentic-os.sh run review-gate [--scope ...] [--diff <f>] [--apply-patch <f>]
./scripts/agentic-os.sh run final-gate

./scripts/agentic-os.sh autonomy [start | stop | pause | resume | status | preflight | follow | bootstrap]
./scripts/agentic-os.sh schedule [add | list | remove | enable | disable | run-now]
./scripts/agentic-os.sh verifications [list | show | override]
./scripts/agentic-os.sh budget [show | set | reset]
./scripts/agentic-os.sh reports [list | show | diff]
./scripts/agentic-os.sh notifications [test]
./scripts/agentic-os.sh transcripts <id>
./scripts/agentic-os.sh git ensure
./scripts/agentic-os.sh sessions summary <id>
./scripts/agentic-os.sh project [list | show | register]
./scripts/agentic-os.sh coverage [list | check]

./scripts/agentic-os.sh --json <subcommand>   # JSON output
```

Exit codes:

| Code | Meaning                                                       |
|------|---------------------------------------------------------------|
| 0    | Success                                                       |
| 1    | Product failure (e.g. test failed, known-bug still red)       |
| 2    | Infra failure (no Docker, healthcheck timeout, config error)  |
| 64   | Usage error (bad CLI argument)                                |
| 130  | Ctrl-C / SIGINT                                               |

---

## AI models

Configured under `models.{planner,implementer,reviewer,triager}`. Each
entry has:

- `provider`: `claude | codex | antigravity | script` (per role; see
  `models.<role>.fallback` for the failover chain on rate-limit / quota /
  auth errors).
- `command`: argv list (first element must be on `$PATH`)
- `role`: `opus | sonnet | codex | gemini | script`
- `auto_fire` (optional, triager only): run automatically after each
  test suite when `true`.

Roles:

- **planner** — designs decisions, writes `requirements.md`, plans
  phases. Default: Claude Opus.
- **implementer** — writes code under planner direction (specs, init,
  package, verify). Default: Claude Sonnet.
- **reviewer** — gates diffs (correctness + business assumption match,
  argv-only subprocess, no assertion weakening). Default: Codex.
- **triager** — bug **severity** + **priority** assessment, refines
  bug descriptions, cross-checks failed runs against `bugs/`. Default:
  Claude (haiku); Codex secondary; Antigravity (`agy --model
  gemini-3.1-pro-high`) as end-of-limit fallback.

Prompts live in `config/prompts/{planner,implementer,reviewer,triager,
bug-adjudication}.md` — provider-neutral. Switching the underlying
model only requires changing `models.<role>.command`.

Invocation behavior:

- Inserts a row into `model_invocations` (id, task_id, run_id, command JSON,
  exit_code, started_at, finished_at).
- Writes the prompt to `agentic-os-runtime/model-inputs/<id>.txt` after
  `redact_prompt()` (masks literal bearer/token/api_key/secret/password).
- Writes model stdout to `agentic-os-runtime/model-outputs/<id>.txt`.
- Missing binary on `$PATH` → `InfraError` (exit 2).
- Reviewer output must use strict format (`verdict: APPROVE|REJECT` +
  reason + findings + READY); otherwise `ValueError`.

### Skills

Per-role optional prompt fragments under `skills/{provider}/`. Naming
schema: `qc-{provider}-{role}-{name}.md` (e.g.
`skills/gemini/qc-gemini-triager-first-check.md`). The runtime auto-
filters by the active provider for each role, so `config/skills.yml`
can pre-enable all 3 providers (claude/codex/gemini) without spamming
warnings when only one is currently configured.

Toggle on/off through `/skills` in the dashboard or by editing
`config/skills.yml` (`per_role.<role>.enabled`).

**Without any model CLIs on PATH**, the deterministic pipeline (parsers,
generators, gates) still works. Models are only needed for non-
deterministic decisions (planner), non-generic patches (implementer),
strict review verdicts (reviewer), or bug triage (triager).

---

## Security and guardrails

| Rule                                                                  | Enforced by                                  |
|-----------------------------------------------------------------------|----------------------------------------------|
| Dashboard server binds only to `127.0.0.1`                            | `config._check_const("dashboard.host", "127.0.0.1")` |
| Write endpoints disabled by default                                   | `dashboard.enable_write_endpoints=false`     |
| All subprocesses use argv lists only                                  | `runtime/subprocess.py` (no shell=True)      |
| No literal secrets land in prompt files                               | `models.redact_prompt()`                     |
| No literal credentials in GET /api/config                             | `config.redact_secrets()`                    |
| Path traversal blocked in config                                      | `_check_safe_relpath`                        |
| URL scheme limited to http/https                                      | `_check_url`                                 |
| Patches never apply without APPROVE                                   | `gates.merge_patch_if_approved`              |
| `@known-bug` remaining red → exit 1                                   | `run-tests.sh --self-check-known-bug`        |
| `compose down --volumes` requires explicit opt-in                     | `sut_lifecycle.build_compose_argv`           |
| Abandoning a patch is auditable (decisions row + gate artifact)       | `workflows.abandon_patch`                    |

---

## Troubleshooting

Full table: [`docs/troubleshooting.md`](docs/troubleshooting.md).

Most common cases:

| Symptom                                     | Action                                                |
|---------------------------------------------|-------------------------------------------------------|
| `ConfigError: invalid config`               | Read the field list in the message; check optional v2 keys. |
| `POST /api/config` → 403                    | Set `dashboard.enable_write_endpoints: true`.        |
| `sut-start` → exit 2 infra_missing_docker   | Install Docker or disable `sut.autostart`.           |
| `task abandon-patch` → no patch artifact    | Path mismatch. Use `task show <id>` to list paths.    |
| Plan gate REJECT trivial assertion          | Assertion is `response.ok` / `status 2xx`. Tighten it.|
| Generator → missing source_ref              | Plan item lacks `source_refs[]`. Add a docs ref.      |
| Reviewer model → ValueError                 | Output not strict format. Inspect outputs/ file.      |

Logs to inspect:

```bash
agentic-os-runtime/logs/orchestrator.log       # runtime decisions
agentic-os-runtime/logs/dashboard.log          # HTTP server
agentic-os-runtime/logs/subprocess/<run>.log   # individual subprocess calls
agentic-os-runtime/model-inputs/<id>.txt       # post-redact prompt
agentic-os-runtime/model-outputs/<id>.txt      # model stdout
agentic-os-runtime/evidence/<run-id>/          # screenshots, manifests, traces
```

---

## Repo layout

```
.
├── scripts/
│   ├── agentic-os.sh              # CLI wrapper
│   ├── agentic-os/agentic_os/     # Python package
│   │   ├── cli/                   # CLI command modules and parser entry
│   │   ├── routes/                # dashboard HTTP routes and logic
│   │   ├── workflows/             # recovery, run-tests, final-gate stages
│   │   ├── orchestrator.py        # SQLite state + leases
│   │   ├── gates/                 # review/final gate validation & static review
│   │   ├── config/                # YAML configuration schema + validation
│   │   ├── sut_lifecycle.py       # docker compose argv + healthcheck
│   │   ├── openapi.py             # OpenAPI YAML/JSON parser (Subphase 04)
│   │   ├── docs_ingest.py         # local docs reader (Subphase 04)
│   │   ├── sut_discovery.py       # node/python/mixed classifier
│   │   ├── plan_v2.py             # TEST-PLAN.json schema + gate
│   │   ├── generators/
│   │   │   ├── api.py             # Playwright TS API generator
│   │   │   └── ui.py              # Playwright TS UI generator
│   │   ├── results.py             # JUnit/Playwright/Cucumber parsers
│   │   └── models/                # planner/implementer/reviewer/triager wrappers & routing
│   └── ...
├── config/                        # config + role prompts + skills.yml
│   ├── agentic-os.yml             # active config
│   ├── agentic-os.yml.example     # template (with optional v2 fields)
│   ├── skills.yml                 # per-role skill enable/disable
│   └── prompts/                   # planner.md, implementer.md, reviewer.md, triager.md
├── skills/                        # qc-{provider}-{role}-{name}.md
│   ├── claude/                    # planner + implementer (default)
│   ├── codex/                     # reviewer (default)
│   └── gemini/                    # triager (Antigravity fallback)
├── agentic-os-runtime/                   # runtime artifacts (gitignored)
├── docs/                          # ADRs, contracts, guides
├── tests/                         # pytest suite
├── run-tests.sh                   # framework self-test
├── README.md                      # this file (EN base)
└── README_pl.md                   # Polish translation
```

---

---

## License

MIT — see [`LICENSE`](LICENSE). You may copy, fork, modify and redistribute
freely; you must keep the copyright notice and the attribution to the
original project (Quality Cat, https://quality-blog.eu — repo
https://github.com/holi87/Agentic-QA-v1) in all copies.

## Contact

Repo issues or directly to Quality Cat (quality-blog.eu).
