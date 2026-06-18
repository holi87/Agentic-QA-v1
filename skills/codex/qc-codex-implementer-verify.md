---
name: qc-codex-implementer-verify
description: "Run the full Playwright suite (or filtered tag bucket), publish the HTML report + trace, classify failures (test bug vs app bug), trigger bug-logging for app bugs, and auto-fix test bugs through a bounded fix loop."
---

# Skill: qc-codex-implementer-verify

## Communication

${include_preamble}

## Standards

This skill operates under shared conventions — read the canonical source, do not restate it:

- Generated-code structure & idioms (Playwright + TypeScript) — `docs/standards/playwright-ts-standards.md` (§5 assertions/web-first waiting, §6 size limits & the C2 lint gate, §7 tags, §8 security).
- Bug-aware policy (keep failing assertions, file a bug) — `docs/bug-aware-policy.md` §2, §6.

## When to use

- After every implement-* slice (per business slice rhythm).
- T+03:00 (R1 mid-verify), T+05:00 (after UI), T+06:30 (final test gate).
- Before generate-docs and package.
- NOT if implementation incomplete for slice (run implement-* first).

## What to do

1. Pre-check: `npx playwright --version` works; `git status --short` (do NOT auto-stash).
2. Static gate FIRST (the "C2" gate, §6): `npx tsc --noEmit` and `npm run lint` (ESLint) MUST be green before the suite runs — a type error or lint failure blocks the run.
3. Run tests: `npx playwright test` (default) or `npx playwright test --grep '<tag-expr>'` (filtered, e.g. `--grep @smoke`); target one file with `npx playwright test tests/<area>.spec.ts`.
4. Report + reports/ refresh: the run writes `playwright-report/` (HTML) and `reports/results.json` (json reporter); then `./scripts/copy-reports.sh --clean`, `./scripts/extract-last-run.sh` (writes `reports/last-run.json`).
5. Classify each failing test:
   - Tagged `@known-bug` → expected red, no action.
   - Test bug (locator stale, wrong expected value, env config) → fix the spec / Page Object / client.
   - App bug (assertion correct per spec, app violates spec) → log `bugs/BUG-NNN-<slug>.md`, tag the test `{ tag: ['@known-bug', '@bug-NNN'] }`, KEEP assertion.
   - Spec ambiguity → log Info bug, document interpretation in `requirements.md`.
6. Auto-fix loop (bounded): iter 1 classify + fix test bugs + log app bugs; iter 2 re-run, fix remaining; after 2 unresolved iterations → root-cause; after 3 → STOP, escalate to human.
7. Bug-aware: NEVER mass-skip failing tests to 'make green'. Use `test.skip(condition, 'reason')` / `test.fixme(...)` only with explicit justification (e.g. `test.fixme('bug-007 blocks scenario')`).
8. Commit: test fix slice `fix: address test issue in <area>`; bug logging delegated.
9. Update `STATUS.md` with verify run row: timestamp, command, green/red counts, classification breakdown.

## Output

Artifacts written:

- `playwright-report/index.html` — Playwright HTML report; `test-results/**/trace.zip` — trace per failure.
- `reports/results.json` — json-reporter output populated by the run.
- `reports/` — refreshed by `copy-reports.sh`.
- `reports/last-run.json` — summary consumed by triager.
- `reports/summary.md` — human-readable run summary.
- `bugs/BUG-NNN-<slug>.md` — one file per app bug found this run.

State changes:

- Tests newly tagged `{ tag: ['@known-bug', '@bug-NNN'] }` when an app bug is logged.
- `STATUS.md` row appended: `T+NN:NN verify <command> <green>/<red> classified=<counts>`.

Git:

- One commit per test-bug fix slice with prefix `fix:`.
- App-bug logging commits delegated to triager skill.

## Example

An `IMPLEMENTATION_PROGRESS.md` row plus the `STATUS.md` line appended after a run:

```markdown
<!-- IMPLEMENTATION_PROGRESS.md -->
| Area | Tests | Green | Red | Classified |
|---|---|---|---|---|
| orders-api | 8 | 7 | 1 | 1 app-bug (BUG-014) |

<!-- STATUS.md -->
T+03:00 verify `npx playwright test --grep @functional-orders` 7/1 classified=app:1,test:0
```
</content>
</invoke>
