# Operator guide

Status: active

Short instructions for a QA operator who wants to run Agentic OS against
their own application without knowing the internal architecture.

> **Naming.** This guide talks about two layers: **Agentic OS** is the
> orchestrator (CLI, dashboard, skills framework you interact with here);
> **QualityCat** is the QA domain the orchestrator produces output for
> (bug reports, cucumber tag families, generated `pl.qualitycat` Java
> tests). Full glossary in [`AGENTS.md`](../AGENTS.md).

## Requirements

- macOS or Linux.
- Python 3.13 with `PyYAML` (installed in `.venv/`).
- Optional: Docker + `docker compose` for local SUT scenarios.
- Optional: Node.js + Playwright if the generator should emit Playwright
  TS and you want to execute them here (`npx playwright test`).

## First run

```bash
git clone <repo> && cd <repo>
python3 -m venv .venv && source .venv/bin/activate
pip install pyyaml
./scripts/agentic-os.sh init
./scripts/agentic-os.sh up --dashboard-only --foreground
open http://127.0.0.1:8765
```

`init` creates `config/agentic-os.yml` from `agentic-os.yml.example`.
The commented sections `sut.kind`, `sut.base_url`, `sut.openapi`, `sut.docs`,
`sut.credentials`, `sut.tests_dir`, `sut.tests.api.runner`,
`sut.tests.ui.runner` are optional v2 fields — uncomment the ones you
need.

## Configuration modes

| Mode               | File / endpoint                    | Requirement                             |
|--------------------|------------------------------------|-----------------------------------------|
| YAML               | `config/agentic-os.yml`       | None                                    |
| Dashboard (write)  | `POST /api/config`                 | `dashboard.enable_write_endpoints=true` |

POST `/api/config`:
- Returns `403` when write endpoints are disabled.
- Returns `400` when you try to set `dashboard.host != 127.0.0.1`.
- Validates URL scheme (`http`/`https`), blocks path traversal in
  `openapi.sources` / `docs.sources` / `tests_dir`.

GET `/api/config` returns the config with redacted credentials — the env
var value appears as `env:<NAME>`, never in plaintext.

## Online URL SUT (no Docker)

For an already-running site (deployed blog, staging service, etc.) point
the OS at the public URL directly — no `docker compose` needed. Drop
this into `config/agentic-os.yml`:

```yaml
sut:
  root: .
  mode: online               # already running, no lifecycle commands
  compose_file: null
  compose_project_name: online-sut   # placeholder; never invoked when mode=online
  autostart: false
  healthcheck:
    command: ["curl", "-fsS", "-o", "/dev/null", "https://example.com"]
    timeout_seconds: 15
    retries: 5
  test_runner: ./run-tests.sh
  install_shim_allowed: false
  web:
    enabled: true
    url: https://example.com
  api:
    enabled: false           # set true + url when the site exposes one
  tests_dir: tests
  tests:
    ui:
      runner: playwright-ts
```

> `compose_project_name` stays required by the strict config validator even
> in `mode: online`; the value is a placeholder and is never invoked.

In `mode: online`:

- `agentic-os run sut-start` and `sut-stop` are no-ops; the analyzer
  jumps straight to the healthcheck.
- `agentic-os doctor --sut` runs the configured `healthcheck.command`
  exactly as written — make sure it returns 0 for the URL you set.
- `agentic-os run sut-healthcheck` is the only lifecycle command you need
  before `run-tests`.

Write your own task spec (a Markdown file describing what to test).
Create the task with either:

```bash
./scripts/agentic-os.sh task create inbox/your-task.md
# or, equivalently:
cp your-task.md inbox/
./scripts/agentic-os.sh inbox ingest
```

The rest of the pipeline (`task analyze` → `plan` → `candidates` →
`approve-candidate` → `implement-tests` → `review-gate` → `apply-patch` →
`run-tests` → `final-gate`) is the same as the local-Docker flow described
below.

## What Agentic OS delivers today

- `agentic-os doctor --sut --docker --models` runs real probes against the configured SUT, Docker, and model CLIs.
- `agentic-os run sut-start | sut-healthcheck | sut-stop` covers the Docker Compose SUT lifecycle (skipped in `sut.mode: online`).
- Config v2 (`sut.kind`, `sut.base_url`, `sut.openapi`, `sut.docs`, `sut.credentials`, `sut.tests_dir`, `sut.tests.{api,ui}.runner`) loads and validates; `POST /api/config` is gated by `dashboard.enable_write_endpoints` or `serve --full`.
- Analysis pipeline (`openapi.py`, `docs_ingest.py`, `sut_discovery.py`) produces `sut-map.json`, `requirements.md`, `risk-map.md`, `candidate-tests.{md,json}` per task.
- Planner emits structured `TEST-PLAN.json` plus markdown; `validate_plan()` enforces the review gate before generation.
- API + UI generators emit executable Playwright TS specs from approved plan items (status / body assertions, env-only credentials, screenshot + trace on UI failure).
- Result parser handles JUnit / Playwright / Cucumber output and classifies failures into `product_bug` / `known_bug_red` / `infra` / `flaky` / `test_bug` with auto bug markdown.
- Model invocations are argv-only, prompts are redacted, every call lands in `model_invocations`.
- Dashboard exposes the full pipeline (analyze → plan → review candidates → generate → review → apply → run → final-gate), including the full-autonomy session and the inbox/pretask intake pipeline.
- `task abandon-patch <id> --patch <p> --reason <r>` unblocks the final gate after operator review.

Pending / partial: full-operator UI polish for the candidate review table.

The packaged fake-SUT proof fixture lives at `examples/fake-sut/`:
run `python examples/fake-sut/run-rc-proof.py` to walk init → inbox
synthesise → analyse → plan → fake-sut report end to end in a temp
workspace (issue #137). The deterministic half is covered by
`tests/test_fake_sut_proof.py`; the online half (implement-tests +
real run-tests against `examples/fake-sut/server.py`) is documented in
`examples/fake-sut/README.md`.

## Operator flow

```bash
# 1. Configuration
vim config/agentic-os.yml          # or POST /api/config when write=true

# 2. Doctor checks before the first run
./scripts/agentic-os.sh --json doctor --sut --docker --models

# 3. Plan and approve generated tests
./scripts/agentic-os.sh task create inbox/your-task.md
./scripts/agentic-os.sh task analyze <task-id>
./scripts/agentic-os.sh task plan <task-id>
./scripts/agentic-os.sh task candidates <task-id>
./scripts/agentic-os.sh task approve-candidate <task-id> <candidate-id> \
  --expected-assertion "GET /health must return HTTP 200" \
  --cleanup-strategy "read-only endpoint"
./scripts/agentic-os.sh task implement-tests <task-id>
./scripts/agentic-os.sh run review-gate --scope assertion \
  --diff agentic-os-runtime/patches/<task-id>/<patch>.patch \
  --work-item <task-id>
./scripts/agentic-os.sh run review-gate --scope assertion \
  --diff agentic-os-runtime/patches/<task-id>/<patch>.patch \
  --apply-patch agentic-os-runtime/patches/<task-id>/<patch>.patch \
  --work-item <task-id>

# 4. Local SUT (if you have docker-compose.yml)
./scripts/agentic-os.sh run sut-start
./scripts/agentic-os.sh run sut-healthcheck
./scripts/agentic-os.sh run run-tests --work-item <task-id>
./scripts/agentic-os.sh run sut-stop

# 5. Final gate
./scripts/agentic-os.sh run final-gate --work-item <task-id>
```

`run-tests` writes `agentic-os-runtime/runs/<run-id>/triage.json` and
`agentic-os-runtime/runs/<run-id>/triage.md`. Exact-spec product failures are
classified and, when `gates.exact_spec_failure_opens_bug=true`, Agentic OS
attempts to create `bugs/BUG-NNN-*.md` with evidence links.

## Ingesting external task documents

Drop free-form documents (`.md`, `.markdown`, `.txt`, `.docx`, `.pdf`) into
`./inbox/` or the staging alias `./pretask/` and process them with:

```bash
./scripts/agentic-os.sh inbox list      # see pending files
./scripts/agentic-os.sh inbox ingest    # one task per document
./scripts/agentic-os.sh inbox synthesize --title "..."  # one task from all pending docs
```

Same pipeline is exposed in the dashboard at `/tasks/new` → **Upload task
document** tile (Upload + Ingest pending + Create task from pending buttons).
`ingest` creates one task per document. `synthesize` reads all pending docs,
extracts source references, requirements, endpoints/pages, known-bug hints and
test-data constraints, then creates one combined task spec. Successfully
processed files are moved to `<intake>/.archive/<stem>-<UTC-ts>.<ext>`;
failures land in `<intake>/.failed/` with a sidecar `*.error.txt` describing
the cause. `.docx` and `.pdf` parsers are optional — install `python-docx` /
`pypdf` to enable them.

**PDF intake is extractable-text-only — scanned PDFs are not OCR'd.** Each
pending PDF is classified at list time as `ok`, `low` or `failed` and the
dashboard shows the badge inline. Scanned PDFs (or PDFs whose text density
falls below ~50 chars/page) are quarantined to `<intake>/.failed/` with a
sidecar that explains the limit. Re-export the source as a text PDF
(e.g. `Print → Save as PDF` from the original editor) or paste the content
into a `.md` / `.txt` instead.

### `Type: public-site` intake — auto-crawler

Tag an intake markdown doc with `Type: public-site` and a `Start URL:` line
to make `inbox synthesize` automatically run the same-origin crawler before
creating the work item:

```markdown
# Public site QA sweep

Priority: P2
SUT root: .
Type: public-site
Start URL: https://staging.example.com/

## Expected behavior
Smoke-crawl the public site, surface broken assets and route inventory.
```

The crawl runs at depth 1 / max-pages 10. Discovered routes are appended to
the rendered spec's "Relevant endpoints or pages" section and broken assets
land in "Known bugs". The full JSON report is persisted under
`agentic-os-runtime/inbox/crawls/<work_item_id>/crawl-NN.json` so downstream
analyze/plan stages can read structured data without re-parsing markdown.

The SSRF guard is on by default — loopback / RFC1918 / link-local targets
are refused (the crawl entry is recorded as `failed` but the task still
ingests). Tests against local HTTP fixtures opt in via the
`allow_private_crawl=True` Python keyword on `synthesize_inbox_task`.

## Dashboard screenshots in CI

The `dashboard screenshots (issue 145)` workflow captures full-page PNGs
of the key operator screens (home, tasks list, new task / inbox, task
detail with candidate review, help / support bundle) on every push and
pull request. The PNGs are uploaded as the `dashboard-screenshots`
artifact (14 day retention) and each one is compared against the
committed Linux baseline under `tests/snapshots/dashboard/linux/`
(issue #166). The gate fails when more than
`AGENTIC_OS_SCREENSHOTS_DIFF_THRESHOLD` percent of pixels differ
(default `1.0`); `*.diff.png` overlays land in the artifact for review.

The pixel-diff gate only runs on Linux by default — committed baselines
were captured on `ubuntu-latest` and macOS/Windows Chromium builds
render fonts differently. Force-enable elsewhere with
`AGENTIC_OS_SCREENSHOTS_GATE=1` (expect non-zero diffs).

Local capture:

```bash
pytest -m browser tests/test_dashboard_screenshots.py
# PNGs land under build/screenshots/. Override with AGENTIC_OS_SCREENSHOTS_DIR.
```

Refreshing baselines after an intentional UI change:

1. Land the UI change on a PR branch.
2. Download the `dashboard-screenshots` artifact from the PR's CI run.
3. Replace the affected files under `tests/snapshots/dashboard/linux/`.
4. Push the baseline update on the same PR.

Or bootstrap in-place from a Linux runner with
`AGENTIC_OS_SCREENSHOTS_BOOTSTRAP=1 pytest -m browser
tests/test_dashboard_screenshots.py`.

## Support bundle for triage

Build a redacted diagnostic tarball when filing an issue or sharing a
reproduction:

```bash
./scripts/agentic-os.sh support-bundle
# → agentic-os-runtime/support-bundles/support-YYYYMMDDTHHMMSSZ.tar.gz
```

Flags (issue #180):

- `--dest <path>` — write outside the runtime directory.
- `--include <list>` / `--exclude <list>` — pick subsystems from
  `config,doctor,events,runs,bugs`. Mutually exclusive.
- `--no-redact` — embed the config file verbatim. Manifest records the
  choice (`redacted: false`); only use when you own the bundle's destination.
- `--tag <name>` — suffix the filename for easier sharing of repeated
  bundles from the same triage session.

Same flow is exposed in the dashboard at `/help` → **Support bundle** tile
(write-gated by `dashboard.enable_write_endpoints`). The dashboard always
applies the default subsystem set with redaction on; the CLI is the
surface that exposes the flags.

The bundle contains:

- `MANIFEST.json` with per-file sizes and truncation flags;
- `config/agentic-os.yml` redacted — leaf keys matching the secret denylist
  (`api_key`, `token`, `password`, `bearer`, `credential`, `client_secret`, …)
  are replaced with `<redacted>`;
- `doctor.json` from `agentic-os doctor --sut --models --docker`;
- `events/*.jsonl` — tail of each runtime event log;
- `runs/<latest>/...` — last run's manifest + small artifacts (per-file cap
  256 KiB; oversize files are truncated, manifest records the original size);
- `bugs/*.json` and `*.md` on disk.

**Review the bundle before sending.** The denylist is conservative, not
exhaustive — run artifacts, events and bug notes are included verbatim and
may contain operator-sensitive data.

## Abandon a pending patch

```bash
./scripts/agentic-os.sh task abandon-patch <task-id> \
  --patch agentic-os-runtime/patches/<task>/<run>/files/x.diff \
  --reason "rejected after operator review; tracked in BUG-007"
```

The patch stays in history, the decision goes to `decisions`, and the
final gate passes.

## Cross-run learnings

The runtime keeps a small store of advisory hints distilled from history
(flaky scenarios, provider quality, skill failures, coverage gaps). They are
hints only — the gates still decide. The planner uses `flaky` hints to
quarantine scenarios; the provider router prefers historically-better
providers per role.

```bash
./scripts/agentic-os.sh learnings list [--kind flaky]
./scripts/agentic-os.sh learnings show <id>
./scripts/agentic-os.sh learnings forget <id>     # operator override
```

Weights decay over time (`decay_tau_days = 14`) and rows below the floor
(`min_weight = 0.05`) are pruned. Run the decay sweep nightly via the
scheduler so the store stays fresh:

```bash
./scripts/agentic-os.sh schedule add learnings-decay \
  --cron "0 3 * * *" --action "learnings decay"
```

## Projects

The runtime addresses work by project (#288). A single `default` project
always exists — its `sut_root` mirrors `sut.root` from config — so a
single-SUT checkout needs no setup. Register more projects to keep their work
items (and, via #289, per-project memory) isolated:

```bash
./scripts/agentic-os.sh project list
./scripts/agentic-os.sh project register "Quality Cat" --sut-root sites/qc
./scripts/agentic-os.sh project show quality-cat
```

The active project resolves by precedence: an explicit flag, then
`project.active` in `config/agentic-os.yml`, then `default`. New work items
land on the active project; omitting the `project:` config block keeps the
zero-config single-SUT behaviour.

## Recovery after a crash

```bash
./scripts/agentic-os.sh --json run recovery
sqlite3 agentic-os-runtime/state.db "pragma foreign_key_check;"
```

## Reference docs

- [`docs/architecture.md`](architecture.md) — runtime map (modules, work model,
  DB tables, model-role wiring, gate/learnings/memory flows). The compressed
  summary in it is injected into agent prompts (`prompt_context`).
- [`docs/security-trust-boundary.md`](security-trust-boundary.md) — dashboard
  auth, `/files/` serving, and SUT subprocess trust boundary.
