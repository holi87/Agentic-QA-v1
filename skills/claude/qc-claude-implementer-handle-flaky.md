---
name: qc-claude-implementer-handle-flaky
description: "Quarantine and root-cause an intermittently failing test: tag @flaky, fold into the @known-flaky quarantine lane, log an S3 flaky-investigation bug, and propose a one-sentence root-cause hypothesis. Never mask a real defect as flaky."
---

# Skill: qc-claude-implementer-handle-flaky

## Communication

${include_preamble}

## Standards

This skill operates under shared conventions — read the canonical source, do not restate it:

- Generated-code structure & idioms (Playwright + TypeScript) — `docs/standards/playwright-ts-standards.md` (§5 web-first assertions / no hard waits, §7 tags).
- Bug-aware policy (keep failing assertions, file a bug, never mask a defect) — `docs/bug-aware-policy.md` §2, §6.

## When to use

- A CI run shows a `test(...)` with a fail-pass-fail signature across the last N=3 runs.
- After verify/triage when a failure does not reproduce deterministically.
- NOT for a test that fails every run (that is a deterministic bug — use triager-first-check).

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- the test has no fail-pass-fail signature (deterministic pass or deterministic fail) → `needs_input: flaky_signature`.
- run history has fewer than 3 runs to judge intermittency → `needs_input: run_history`.
- `reports/last-run.json` (or the Playwright run archive) is missing → `needs_input: test_run`.

## What to do

1. Confirm the fail-pass-fail signature over the last 3 runs from the run archive (the Playwright HTML report flags retried/flaky tests; the trace of a failing attempt is the evidence); if absent, STOP.
2. Quarantine: add a `@flaky` tag to the test's `{ tag: [...] }` array in `tests/<area>.spec.ts` and a `@known-flaky` tag so the gating run excludes it via `npx playwright test --grep-invert @known-flaky`, while the quarantine lane runs `--grep @known-flaky`.
3. Log `bugs/BUG-NNN-flaky-<slug>.md` at severity S3 with `component`, the failing `test:` ref, and evidence pointers (`evidence/BUG-NNN/` traces + HTML report for each of the 3 runs).
4. Propose a one-sentence root-cause hypothesis in the bug body (timing/order/shared-state/network), labelled `Hypothesis:`; point the fix at §5 — replace any hard wait with a web-first auto-retrying `await expect(locator).toBeVisible()` and lean on Playwright `retries` rather than the clock.
5. NEVER convert a deterministic failure to `@flaky` to hide a real defect — that is a bug-aware-policy violation.
6. Commit: `git add tests/ bugs/ && git commit -m 'test: quarantine flaky <slug> + log BUG-NNN'`.

## Output

- Spec diff: the `test(...)` gains `@flaky` + `@known-flaky` in its tag array; the gating run excludes `@known-flaky` via `--grep-invert`.
- New `bugs/BUG-NNN-flaky-<slug>.md` at S3 with evidence pointers + one-sentence hypothesis.
- Git commit `test: quarantine flaky <slug> + log BUG-NNN`.

## Example

The quarantine diff this skill produces:

```diff
 test('order list refresh shows the latest order',
-  { tag: ['@functional-orders', '@regression'] },
+  { tag: ['@functional-orders', '@regression', '@flaky', '@known-flaky', '@bug-031'] },
   async ({ ordersPage }) => {
```
