---
name: qc-claude-triager-severity-priority
description: "Re-evaluate existing bug files in bugs/, recompute severity (impact × scope) + priority (severity × frequency × business value), update YAML frontmatter accordingly. Use this when business context shifts, new evidence accumulates, or before submission to ensure ratings are calibrated."
---

# Skill: qc-claude-triager-severity-priority

## Communication

${include_preamble}

## When to use

- Periodic re-calibration of existing bugs/.
- Before submission to align bug ratings with current business priorities.
- When new evidence changes the impact or scope of a known bug.
- NOT for new failures (use first-check).
- NOT during implementation slices.

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- the `bugs/` directory is missing or empty → `needs_input: bugs`.
- `docs/severity-policy.md` matrix is missing → `needs_input: severity_policy`.
- a bug frontmatter lacks the `severity`/`component` needed to recompute → `needs_input: bug_metadata`.

## What to do

1. Read every `bugs/BUG-NNN-<slug>.md`. Extract current frontmatter: `severity`, `priority`, `likelihood`, `impact`, `scope`, `frequency`, `business_value`, `status`, `component`, `owasp`, `iso25010`.
2. Re-read each bug's Steps to Reproduce + Expected + Actual + Evidence sections.
3. Recompute SEVERITY using impact × scope rubric (per bug-reporting.md):
   - Critical (S1): auth bypass, data loss, security breach, system unusable, scope = all users.
   - High (S2): major feature broken on happy path, IDOR, broken CRUD, wrong total in financial calc, scope = significant user segment.
   - Medium (S3): workaround exists, wrong error message, partial loss, scope = limited.
   - Low (S4): cosmetic, off-by-one in non-critical counter, edge case, scope = rare.
   - Info: spec ambiguity, suggestion.
4. Recompute PRIORITY using severity × frequency × business value:
   - P1 = Critical/High severity × High frequency × High business value.
   - P2 = High/Medium severity × Medium-High frequency.
   - P3 = Medium severity × Medium frequency.
   - P4 = Low severity × Low frequency.
5. Re-check OWASP/ISO mapping: any new evidence shifts mapping? E.g. a 'broken filter' bug found to leak other users' data → upgrade to OWASP API1 BOLA + Critical.
6. Update YAML frontmatter in-place. Append `## Re-triage History` section row with: ISO timestamp, previous severity → new severity, previous priority → new priority, rationale (1-2 sentences citing evidence).
7. Run `./scripts/new-bug.sh --reindex` to refresh `bugs/README.md` (re-sorted by severity desc).
8. Commit: `git add bugs/ && git commit -m 'docs: re-triage bug severity + priority (<n> bugs updated)'`.

## Output

- Updated YAML frontmatter in every re-triaged `bugs/BUG-NNN-<slug>.md` (`severity`, `priority`, `likelihood`, optionally `owasp`/`iso25010`).
- `## Re-triage History` section appended in each modified bug file.
- Refreshed `bugs/README.md` index (sorted by severity desc).
- Git commit `docs: re-triage bug severity + priority (<n> bugs updated)`.
- Summary report: bugs reviewed count, upgrades, downgrades, no-change.

## Example

A recalibrated `bugs/BUG-NNN-<slug>.md` frontmatter. Parses as YAML:

```yaml
id: BUG-014
title: Paid order cancellation succeeds (should be refused)
severity: S2
priority: P2
likelihood: M
component: API
owasp: API5:2023
iso25010: functional-correctness
found_by: triager-autopilot
scenario: tests/api/orders.spec.ts::a paid order cancellation is refused
```
