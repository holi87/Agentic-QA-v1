# Bug-Aware Policy

Status: active

Phase 06 design contract. Rules below are normative for the orchestrator,
all runtime wrappers, and any review prompt. Implementations that change
test assertions to make a failing test green violate this policy and must
fail the affected gate.

Cross-references: `docs/severity-policy.md`, `docs/standards/bug-reporting.md`
(after migration in phase 07), `docs/runtime-contract.md`,
`scripts/assertion-guard.py`, gates in `config/agentic-os.yml` (legacy `.qualitycat/agentic-os.yml` is fallback only)
(`exact_spec_failure_opens_bug`, `assertion_changes_require_decision`,
`known_bugs_fail_exit`).

## 1. Source of truth precedence

When the SUT and the documentation disagree, the policy resolves the
conflict in this fixed order:

1. **Locked operator decision** recorded in the `decisions` table.
2. **Business requirements** in `requirements.md` (or upstream spec).
3. **OpenAPI / interface contract** shipped with the SUT.
4. **Observed SUT behaviour**.

Whenever 2 and 3 disagree, the orchestrator must open a `decision`
blocker before allowing a related test to pass or fail silently.

## 2. Exact-spec failure rule

A scenario is an *exact-spec failure* when:

- It comes from a feature file that maps 1:1 to a requirement clause, and
- The failure message points at the assertion that encodes that clause.

For every exact-spec failure the orchestrator MUST:

1. Keep the assertion unchanged.
2. Run `qualitycat.file_bug(...)` which creates
   `bugs/BUG-NNN-<slug>.md`, an evidence dir under `agentic-os-runtime/evidence/`,
   and inserts a `bugs` row with `status='open'`.
3. Re-tag the scenario `@known-bug @bug-NNN` and add it to
   `bugs/README.md` (open the file in append mode, never rewrite).
4. Keep `run-tests.sh` exit code at `1` (gate
   `known_bugs_fail_exit: true`).
5. Emit event `bug.filed` with payload
   `{bug_id, severity, scenario, requirement_ref}`.

Never delete `@known-bug` without an explicit human decision recorded in
`decisions` (`decided_by='operator'`).

## 3. Conflict: OpenAPI vs business requirement

When the OpenAPI schema contradicts a business requirement:

1. The implementer (Sonnet) must NOT generate the test from OpenAPI alone.
2. The orchestrator opens a `decision` blocker (severity per section 4
   of `docs/severity-policy.md`) with `source='requirements_vs_openapi'`.
3. Operator answers via `decisions` row. The orchestrator stores
   `decided_by`, `rationale`, `consequences`.
4. The test is generated from the chosen source and references the
   `decision_id` in its docstring/comment.

If no decision arrives within the contest budget, the orchestrator must
default to the *business requirement* and mark the test scenario
`@requires-decision`, never falsely green.

## 4. Severity routing

| Severity | Auto-file bug | Interrupt operator | Block phase cut |
|---|---|---|---|
| P0 | yes | yes (immediate) | yes |
| P1 | yes | yes (within 5 min) | yes if open at cut |
| P2 | yes | no | no |
| P3 | yes (low priority) | no | no |

Detailed severity definitions live in `docs/severity-policy.md`. The
orchestrator MUST query `gates.assertion_changes_require_decision` and
`gates.exact_spec_failure_opens_bug` before acting.

## 5. Scope cut policy (5h window)

Trigger a `VERIFY_TRIAGE` phase cut when ANY of these is true:

1. ≥4 open blockers at severity ≥ P2.
2. Remaining contest budget < 75 minutes AND no API feature area has
   green coverage.
3. ≥3 consecutive `IMPLEMENT` runs returned `failure_kind='infra'`.
4. The dashboard reports `bugs.open + blockers_open > 8`.

On `VERIFY_TRIAGE`:

- Suspend new implementation tasks (`IMPLEMENT`, `DESIGN`).
- Finalise reports for whatever is green or already filed.
- Run `qualitycat.copy_reports` + `qualitycat.build_summary`.
- Open a final `decision` blocker `severity=P0` asking the operator
  whether to ship.

## 6. Assertion immutability gate

`scripts/assertion-guard.py` is the canonical enforcement. Restated for
this policy:

- Any patch that weakens an assertion (regex replacement, range widening,
  `assertTrue(true)` insertion, `expect(...).toBeDefined()` stripped to
  `expect(...).toBeTruthy()` and similar) is REJECTED unless an
  `assertion_changes` row with `status='allowed'` and a linked
  `decisions.id` exists.
- Strengthening (narrower expected value, stricter regex) is allowed.
- The orchestrator records every detected change as an
  `assertion_changes` row regardless of outcome; this row is the audit
  trail for review gates.

There is no policy path that allows changing an assertion *only* to make
a red test green. Patches doing that must be REJECTED by Codex review
(prompt: `config/prompts/codex-reviewer.md`).

## 7. Operator interruption budget

Hard cap: at most 4 interruptions per contest hour. If the queue exceeds
this, downgrade severity to P2 and continue filing bugs instead of
asking. Reset the counter every hour. Always interrupt for P0.

## 8. Forbidden actions

The orchestrator MUST reject (with `error_class='policy_violation'`) any
task that asks to:

- Modify SUT source files (any path under `sut.root` per config).
- Remove `@known-bug` or `@bug-NNN` tags from a green-on-red test.
- Skip report generation when `reports.require_reports_on_failure=true`.
- Promote a test to green by changing the assertion when an open
  `bugs` row references that scenario.

Each rejected task emits event `policy.violation_rejected` and writes a
row in `blockers` with `severity='P1'`, `source='policy'`.
