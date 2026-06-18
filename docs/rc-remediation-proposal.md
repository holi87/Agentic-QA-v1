# RC remediation proposal

Status: superseded
Superseded-on: 2026-05-22
Superseded-by: ../README.md (Status section), GitHub issue tracker
Reason: this is a 2026-05-20 remediation roadmap paired with the RC readiness
analysis. Most named workstreams (crawler, candidate review bulk UI, runtime
migrate, browser harness, skill eval golden) have since landed on `main`.
Treat as historical context only.

Date: 2026-05-20
Branch: `task/rc-readiness-analysis`

## Goal

Move Agentic OS from an implementation preview to a real Release Candidate for
this operator promise:

- configure a SUT from YAML or dashboard,
- discover API/UI test candidates from task specs, OpenAPI, docs, and SUT shape,
- let the operator approve or reject generated test candidates,
- generate executable automated web/API tests,
- run the tests through the configured runner,
- classify failures,
- create or update bugs with evidence,
- publish human-readable task/run reports,
- make the same flow available from CLI and dashboard.

## P0: Make the readiness signal truthful

### Fix `doctor`

Required changes:

- check `config/agentic-os.yml` as the canonical config path;
- keep legacy `.qualitycat/agentic-os.yml` only as an explicit compatibility
  note, not as the main `config_exists` signal;
- include the `triager` model role in `--models`;
- report missing Docker compose file as either:
  - `error` when `sut.mode=local`, or
  - `not_applicable` when `sut.mode=online`;
- report empty OpenAPI/docs/test runner fields as actionable warnings, not as
  hidden absence.

Acceptance:

```bash
./scripts/agentic-os.sh init
./scripts/agentic-os.sh --json doctor --sut --docker --models
```

Expected result: the command reports the canonical config as present and shows
clear readiness status per SUT mode and model role.

### Fix phase/status visibility

The status command should not imply that implementation phases are merely
`planned` when code and docs already exist. Either migrate phase state or hide
phase readiness from the operator dashboard until it is reliable.

## P0: Close analysis -> plan -> generator

### Promote candidates into plan items

Today, `task analyze` can find candidates, but `TEST-PLAN.json` can be empty.
That is the main RC blocker.

Required changes:

- persist analysis candidates in a structured schema;
- convert API/UI candidates into `PlanItem` records;
- default uncertain candidates to `needs_operator_decision`;
- allow safe candidates to be marked `generate_now` only when:
  - OpenAPI/docs/spec source references are available,
  - the assertion is specific enough,
  - no write/destructive operation is generated without cleanup or approval.

Acceptance:

```bash
./scripts/agentic-os.sh --json task analyze <task-id>
./scripts/agentic-os.sh --json task plan <task-id>
```

Expected result: `TEST-PLAN.json` contains concrete plan items matching the
candidate summary. It must not silently produce an empty plan when candidates
exist.

### Add CLI approval commands

Add explicit commands for candidate decisions:

```bash
./scripts/agentic-os.sh task candidates <task-id>
./scripts/agentic-os.sh task approve-candidate <task-id> <candidate-id>
./scripts/agentic-os.sh task reject-candidate <task-id> <candidate-id> --reason "..."
./scripts/agentic-os.sh task mark-needs-decision <task-id> <candidate-id> --reason "..."
```

The generator should consume only approved `generate_now` items.

### Add dashboard candidate review

Dashboard task detail should include a candidate table:

- source: task spec, OpenAPI, docs, SUT discovery;
- area: API/UI/security/accessibility/contract;
- endpoint or route;
- proposed assertion;
- risk level;
- source reference;
- decision buttons: generate now, needs decision, reject;
- reason field.

## P0: Make generated tests executable by default

### Treat skeleton-only output as not generated

`implement-tests` should not return a success-looking result when it only emits
a Markdown skeleton unless the operator explicitly selected skeleton mode.

Required behavior:

- if plan has no executable items: return `needs_operator_decision`;
- if generator emits no executable files: return non-success status in JSON;
- include the reason and next operator action.

### Strengthen generator assertions

Weak fallback assertions should not be generated automatically.

Required behavior:

- API tests need expected status, schema/body assertion, or explicit operator
  approval for smoke-only assertions.
- UI tests need a semantic target, visible text, accessibility role/name, or
  explicit operator approval for navigation-only checks.
- Destructive API operations require cleanup, test data strategy, or explicit
  skip with reason.

## P0: Wire tests -> reports -> bugs

### Add a result triage workflow

After `run-tests`, Agentic OS should parse available reports and produce a
structured triage artifact:

```text
agentic-os-runtime/runs/<run-id>/triage.json
agentic-os-runtime/runs/<run-id>/triage.md
```

Each failure should have:

- classification: product_bug, known_bug_red, infra, flaky, test_bug,
  inconclusive;
- source scenario/test;
- evidence links;
- proposed severity and priority;
- suggested bug title/body;
- operator action state.

### Auto-create bug records for exact-spec product failures

When a failure is classified as exact-spec product bug:

- create or update `bugs/BUG-NNN.md`;
- copy or link evidence under `evidence/`;
- add known-bug tagging guidance for the test;
- keep the test red until the product behavior changes or an explicit policy
  decision is recorded.

Acceptance:

```bash
./scripts/agentic-os.sh run run-tests
./scripts/agentic-os.sh run final-gate
```

Expected result: failures are classified, human-readable reports exist, and
product bugs have bug files with evidence.

## P1: Complete dashboard-managed operator flow

### Add a guided SUT configuration wizard

The dashboard should guide the operator through:

- local Docker vs online SUT;
- web URL and API base URL;
- healthcheck command or URL;
- OpenAPI source;
- docs source;
- credentials as environment variable references;
- test output/report locations;
- API and UI runner commands.

The wizard should run live validation and save only valid config.

### Add a single task execution page

The task page should show the full lifecycle:

1. Task spec.
2. SUT/config readiness.
3. Analysis artifacts.
4. Candidate review.
5. Generated patch review.
6. Apply/abandon patch.
7. Run tests.
8. Failure triage.
9. Bug records.
10. Final gate.

Every step should be runnable from dashboard and mirrored by CLI.

## P1: Implement or rename autonomy

Current autonomy covers only part of the journey. For RC, either:

- implement an orchestrator loop that can run analyze -> plan -> candidate
  approval checkpoint -> implement -> review -> apply -> run -> triage ->
  final gate, or
- rename and document current autonomy as "analysis/generation assistant", not
  full Agentic OS autonomy.

The command `up` should not imply a daemon exists unless it is actually
implemented.

## P1: Add RC proof tests

Add an end-to-end fake SUT proof that runs in CI/local validation.

Minimum fixture:

- a tiny fake SUT with one API endpoint and one UI route;
- one expected pass;
- one exact-spec product failure;
- one known-bug-red case;
- OpenAPI file and minimal docs;
- Agentic OS config pointing to this fixture.

Required proof:

```bash
./scripts/agentic-os.sh init
./scripts/agentic-os.sh --json doctor --sut --docker --models
./scripts/agentic-os.sh --json task create --spec tests/fixtures/rc-task.md
./scripts/agentic-os.sh --json task analyze <task-id>
./scripts/agentic-os.sh --json task plan <task-id>
./scripts/agentic-os.sh --json task approve-candidate <task-id> <candidate-id>
./scripts/agentic-os.sh --json task implement-tests <task-id>
./scripts/agentic-os.sh --json task review-gate <task-id>
./scripts/agentic-os.sh run run-tests
./scripts/agentic-os.sh run final-gate
```

Acceptance:

- executable Playwright files are generated;
- reports are created even on failure;
- exact-spec product failure creates a bug;
- known-bug scenario remains red;
- dashboard can show the same run and artifacts.

## P2: Operator polish

Recommended follow-up improvements:

- make `task create` accept absolute spec paths by copying them into a safe
  runtime location, or document the repo-relative requirement more clearly;
- implement `logs --follow`;
- implement or remove `install-shim` from visible help;
- make `down` behavior explicit for dashboard-only and daemon modes;
- add examples under `examples/` for online API, local Docker web app, and
  mixed API/UI SUT;
- add a dashboard "copy support bundle" action that collects config redacted,
  doctor output, task artifacts, last run, bug links, and logs.

## Suggested implementation milestones

| Milestone | Scope | Estimate |
|---|---|---:|
| RC-0 Truthful operator surface | Fix doctor/config/status signals and visible limitations. | 1-2 days |
| RC-1 Candidate promotion | Persist candidates, approve/reject flow, non-empty plan guarantees. | 2-4 days |
| RC-2 Executable generation | Make `implement-tests` produce runnable specs or explicit needs-decision status. | 2-3 days |
| RC-3 Run to bug/report | Classify failures and create/update bug/evidence records. | 2-3 days |
| RC-4 Dashboard E2E | SUT wizard and task lifecycle page. | 3-5 days |
| RC-5 RC proof | Fake SUT and external sample SUT validation. | 2-3 days |

## Final RC acceptance checklist

Before calling this repository RC, require:

- `python -m pytest` is green.
- `git diff --check` is clean.
- `./scripts/agentic-os.sh --json doctor --sut --docker --models` gives a
  truthful readiness result.
- dashboard `/healthz`, `/api/status`, `/api/config` smoke passes.
- one CLI flow generates executable API/UI tests from approved candidates.
- one dashboard flow performs the same lifecycle.
- `./run-tests.sh --self-check-known-bug` returns `1` and still writes reports.
- an exact-spec fake SUT failure creates a bug with evidence.
- final gate blocks when reports, bug triage, or patch decisions are missing.
