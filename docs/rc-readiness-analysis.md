# RC readiness analysis

Status: superseded
Superseded-on: 2026-05-22
Reason: the gap inventory below was a point-in-time snapshot from 2026-05-20
and most of the named blockers (browser regression, skill eval golden,
crawler core, legacy runtime migrate, CLI stubs, candidate review UI bulk,
inbox PDF extraction status, …) have since been resolved on `main`. Treat
this file as historical context only. The current readiness picture lives
in `README.md` ("Status" section) and the open issue tracker; do not make
RC decisions from this document.

Date: 2026-05-20
Branch: `task/rc-readiness-analysis`

## Executive verdict

Agentic OS is **not ready to be treated as a product Release Candidate** for the
full promise:

> A dashboard- or CLI-managed local Agentic OS that can be configured for a SUT,
> discover web/API test needs, write executable automated tests, run them, file
> bugs, and publish human-readable reports.

The repository is a strong internal RC for the runtime skeleton, dashboard
backend, configuration primitives, SUT lifecycle helpers, test runner reporting,
and generator components. The missing part is the integrated operator journey:
from a configured real SUT and task description to approved executable tests,
test execution, bug filing, and final report without hand-editing internal
artifacts.

Recommended RC label today: **implementation preview / internal RC**, not
external RC.

## Readiness score

| Area | Score | Assessment |
|---|---:|---|
| CLI control | 70% | Core commands exist, but important flows are still partial or stubbed. |
| Dashboard control | 65% | Dashboard and APIs exist; write mode is gated; full E2E operator UX is incomplete. |
| Configuration | 60% | `config/agentic-os.yml` exists and can be edited, but defaults are not enough for a real SUT and doctor reports are partially misleading. |
| SUT lifecycle | 70% | Docker compose start/stop/healthcheck exists, but the default repo config fails because `docker-compose.yml` is missing. |
| Test discovery and planning | 55% | Analysis can produce candidates, but candidate promotion into executable plan items is incomplete. |
| Test generation | 45% | Generators can emit Playwright specs from explicit `PlanItem(generate_now)` input, but the normal CLI smoke flow emitted no executable tests. |
| Test execution and reports | 80% | Runner/report scripts work and preserve known-bug-red behavior. |
| Bug filing and triage | 55% | Bug/report primitives exist, but exact-spec failure to bug/evidence flow is not wired end-to-end. |
| Model/provider integration | 60% | Provider prompts/skills and model invocation primitives exist; the main operator path is still mostly deterministic. |
| RC operability | 58% | Useful for controlled internal demos; not yet a reliable one-command or dashboard-driven RC. |

Overall readiness: **58/100 - BLOCK for external RC**.

## Evidence collected

Commands and observations from the local repository:

| Check | Result |
|---|---|
| `git fetch origin main && git pull --ff-only origin main` | Fresh `main` was pulled before creating this task branch. |
| `./scripts/agentic-os.sh init` | Succeeded; runtime root `agentic-os-runtime`, config path `config/agentic-os.yml`. |
| `./scripts/agentic-os.sh --json doctor --sut --docker --models` | Ran successfully, but reported `config_exists=false` even though `config/agentic-os.yml` exists. |
| Doctor SUT check | Reported `compose_file missing: docker-compose.yml`, so the default local SUT configuration is not runnable as-is. |
| Doctor model check | Found configured planner/implementer/reviewer binaries, but the triager role is not part of this probe. |
| `./scripts/agentic-os.sh --json status` | Runtime and SQLite state are usable, but phase status is not a trustworthy RC progress indicator. |
| `python -m pytest` | `236 passed`. |
| `./run-tests.sh --self-check-known-bug` | Returned `1` and still produced `reports/last-run.json` and `reports/summary.md`; known bugs remain red. |
| Dashboard smoke on port `8876` | `/healthz`, `/api/status`, and `/api/config` returned valid JSON. |
| CLI task smoke: create/analyze/plan/implement-tests | Created a work item and candidate summary, but `TEST-PLAN.json` had `0` items and `generated_v2.skipped=true` with reason `no_items`. |

## What works today

The repository has a real Agentic OS foundation:

- `scripts/agentic-os.sh` is the default CLI entrypoint.
- `init`, `doctor`, `status`, `up --dashboard-only`, task commands, run
  commands, and recovery-oriented commands exist.
- Runtime state is local and SQLite-backed under `agentic-os-runtime`.
- The dashboard server exposes status, configuration, task actions, SUT actions,
  git actions, agent views, skill views, suggestions, and autonomy endpoints.
- Dashboard configuration writes are protected by write-mode gating and localhost
  constraints.
- Config v2 fields exist for SUT mode, URLs, OpenAPI/docs sources,
  credentials, test directories, and per-area runners.
- SUT lifecycle helpers support Docker Compose start/stop/healthcheck and online
  mode no-op start/stop.
- The work-item pipeline can create tasks, analyze task specs, write analysis
  artifacts, write a test plan, create patch artifacts, run review/final gates,
  and execute the configured test runner.
- OpenAPI and docs ingestion modules exist.
- API/UI Playwright generators exist and are covered by unit tests.
- Result parsing/classification exists for JUnit, Playwright, and Cucumber
  formats.
- Bug and report scripts exist: `new-bug.sh`, `copy-reports.sh`,
  `extract-last-run.sh`, and `build-summary.sh`.
- The runner creates reports even when tests return non-zero.
- Known product bugs can intentionally remain red.
- Provider-neutral prompts and provider-specific skills are present.

## Blocking gaps

### 1. Automatic test generation is not integrated end-to-end

The strongest RC blocker is the gap between analysis candidates and executable
tests.

In the smoke flow, `task analyze` reported API/UI candidates, but `task plan`
produced a `TEST-PLAN.json` with zero items. Then `task implement-tests`
created only a Markdown skeleton patch under `tests/generated/...spec.md` and
skipped the v2 executable generator with `reason=no_items`.

That means the current default operator flow can appear successful while not
creating runnable web/API tests.

### 2. Candidate approval is not an operator-grade dashboard flow

The generator expects plan items with `decision=generate_now`, but the normal
flow does not yet provide a clear dashboard/CLI approval path that turns
analysis candidates into executable generation decisions.

This makes the system usable by maintainers who know the internals, but not by a
QA operator expecting a guided Agentic OS.

### 3. Dashboard management is partial

The dashboard works as a local control surface, but it is not yet a complete
100% management layer:

- `up` currently requires `--dashboard-only`; the orchestrator daemon path is
  not implemented.
- Dashboard write endpoints are correctly gated, but this also means the default
  dashboard is not a full management surface unless started in full/write mode.
- The dashboard API exposes task actions, but the complete UX for configure,
  analyze, approve candidates, generate tests, apply patches, run, triage, and
  final gate is not yet proven as one operator journey.

### 4. Configuration readiness is weaker than the product promise

The repo has `config/agentic-os.yml`, but the default config is not enough to run
a local SUT:

- `sut.compose_file` defaults to `docker-compose.yml`, which is missing in this
  repo.
- important v2 fields such as OpenAPI/docs/test runner details are empty unless
  the operator fills them.
- `doctor` currently reports `config_exists=false` because it checks the legacy
  location instead of the canonical config file.
- the triager model role is part of the architecture, but the doctor model probe
  does not verify it.

The system can be configured, but the readiness signal is not yet reliable
enough for RC.

### 5. Exact-spec failure to bug filing is not wired as one flow

The repo has building blocks for bug-aware behavior:

- result parsing and classification,
- bug markdown rendering,
- bug creation script,
- evidence/report generation,
- known-bug-red behavior.

However, the normal `run-tests` workflow does not yet prove the complete path:
test failure -> classify product bug vs known bug vs infra/test issue -> create
or update `bugs/BUG-NNN` -> attach evidence -> show dashboard triage -> final
gate decision.

For the Agentic OS promise, this is a release blocker.

### 6. Generator fallback assertions are too weak for strict QA

The API/UI generators can emit executable tests, but fallback assertions such as
"not 5xx" or "URL is not error/404/500" are weaker than exact business
assertions.

For an RC, weak fallback assertions should require operator approval or be
classified as `needs_operator_decision`, not silently become generated tests.

### 7. Some CLI capabilities are explicitly incomplete

The code inspection found intentionally incomplete or partial capabilities,
including:

- orchestrator `up` without dashboard-only mode,
- `down` for the not-yet-implemented daemon path,
- `logs --follow`,
- `install-shim`.

These are acceptable in an internal preview, but they should be visible as
non-RC limitations.

## Product answer to the user's question

Does the repo currently meet the stated requirement?

**No, not fully.**

It can be configured and controlled locally through CLI/dashboard primitives. It
can run tests and produce readable reports. It has the components needed to
generate API/UI tests and manage bug-aware outcomes. But it does not yet prove
the full RC-grade flow where an operator configures a SUT, lets the system
discover test needs, approves them, generates executable web/API tests, runs
them, files bugs, and receives final human-readable reports from dashboard or
CLI without knowing internal artifact formats.

## RC gate recommendation

Do not call the current repository an external RC until all of the following are
true:

- `doctor --sut --docker --models` is truthful and checks the canonical config
  and all configured roles.
- a sample SUT can be configured from dashboard or YAML.
- analysis candidates become reviewable plan items.
- approved plan items generate real Playwright API/UI tests.
- generated patches can be reviewed, applied, run, and final-gated from CLI and
  dashboard.
- failed exact-spec tests create or update bug records with evidence.
- reports are human-readable and linked from the task/dashboard.
- a single RC smoke test proves the full journey on a fake SUT.
