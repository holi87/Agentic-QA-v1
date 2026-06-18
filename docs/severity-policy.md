# Severity Policy

Status: active

Phase 06 design contract. Defines `severity` values used in the
`bugs`, `blockers`, and `decisions` tables, the dashboard, and every
prompt that adjudicates failures.

Cross-references: `docs/bug-aware-policy.md`,
`docs/database-schema.md`, `docs/standards/bug-reporting.md` (post-phase-07
migration), `config/prompts/bug-adjudication.md`.

## 1. Severity scale

Four discrete values. Do not add intermediate ones; the database CHECK
constraint enforces this set.

| Code | Label | Definition |
|---|---|---|
| `P0` | Critical | SUT unusable, evidence of data loss, authentication bypass, security breach, or any defect that would have stopped the contest itself if found in production. |
| `P1` | High | Major business flow broken, wrong total/state in a happy-path scenario, missing validation on a critical field, evidence of regressions across multiple scenarios. |
| `P2` | Medium | Workaround exists, secondary flow broken, wrong error code on an edge case, observable but non-blocking spec violation. |
| `P3` | Low | Cosmetic or marginal: typo in a label, off-by-one in non-critical counter, formatting issue, undocumented edge case. |

`Info` (spec ambiguity) from the legacy standard maps to a
`blockers` row with `severity='P2'`, `source='requirements_clarification'`.
It is never a `bugs` row.

## 2. Severity = Impact × Likelihood

Use the matrix below; pick the worst cell that the scenario reasonably
exercises.

| Impact ↓ / Likelihood → | High (happy path) | Medium (specific input) | Low (edge combo) |
|---|---|---|---|
| Catastrophic (data loss, auth bypass) | P0 | P0 | P1 |
| Major (broken business flow)         | P1 | P1 | P2 |
| Minor (workaround available)         | P2 | P2 | P3 |
| Cosmetic                              | P3 | P3 | P3 |

When in doubt between two adjacent buckets, choose the higher severity
during the contest and let the operator downgrade on review.

## 3. Routing rules

Used by the orchestrator and `qualitycat.file_bug`:

| Severity | File bug auto | Interrupt operator | Block phase cut | SLA to acknowledge |
|---|---|---|---|---|
| P0 | yes | yes, immediate | yes | inside 1 min |
| P1 | yes | yes, ≤5 min | yes if still open at cut | inside 5 min |
| P2 | yes | no (queue only) | no | end of phase |
| P3 | yes | no | no | end of contest |

If the operator interruption budget for the hour (4 per
`docs/bug-aware-policy.md` §7) is exhausted, P1 still interrupts on the
hour rollover, while P2/P3 stay queued.

## 4. When to ask the operator (not just file)

Ask, do not auto-file, when ANY of these holds:

1. The failure source cannot be tied to a requirement clause (no
   reference in `requirements.md`, no OpenAPI path).
2. The same test scenario already has 2 distinct `bugs` rows in this
   contest — likely indicates a flaky test or moving target.
3. The patch under review would weaken an assertion (see
   `docs/bug-aware-policy.md` §6).
4. The blocker is severity P0 or P1 and the orchestrator has no
   automated remediation in the current phase.

The operator answers via a `decisions` row. The orchestrator stores
`decided_by='operator'`, the topic, rationale, and consequences.

## 5. Severity propagation

- `bugs.severity` cascades to the test scenario tags
  (`@severity-P0` … `@severity-P3`) when re-tagging in §2 of
  `bug-aware-policy.md`.
- `blockers.severity` controls dashboard colour (`P0` red, `P1` amber,
  `P2/P3` neutral) and whether `recovery_scan` raises the runtime state
  to `degraded`.
- `decisions.severity` is implicit — a decision linked to a P0 bug is
  itself P0 for cut purposes.

## 6. Scope cut interaction

A phase cut decision (`VERIFY_TRIAGE`) MUST consider open severities:

- Any open P0 forces the cut answer to "block ship until resolved or
  operator acknowledges risk".
- Any open P1 forces an explicit `decisions` row before ship
  (`topic='ship_with_open_P1'`).
- P2 and P3 require no extra decision but appear in the readiness
  summary.

## 7. Severity downgrade workflow

Severity can only be reduced when:

1. The operator records a `decisions` row with
   `topic='severity_downgrade'`, naming the bug id and the new severity.
2. The `bugs.severity` UPDATE happens inside the same transaction as the
   `decisions` INSERT, so the audit trail cannot drift.

Automatic downgrades are forbidden.
