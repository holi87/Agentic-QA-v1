---
name: qc-codex-implementer-package
description: "Finalize the project for submission — security audit, panic-mode if late, final commit, ZIP if required, STATUS.md sealed. Requires a final-gate GO before user submits."
---

# Skill: qc-codex-implementer-package

## Communication

${include_preamble}

## Standards

This skill operates under shared conventions — read the canonical source, do not restate it:

- Generated-code structure & idioms (Playwright + TypeScript) — `docs/standards/playwright-ts-standards.md` (§7 tags / `--grep` selection, §8 security: env-injected secrets, pinned `npm ci`, no logged credentials).

## When to use

- T+07:45 — LAST skill before user submits.
- After generate-docs and final verify.
- BEFORE final-gate (BLOCKING — needs GO verdict from reviewer).
- NOT mid-implementation; NOT if verify shows unaddressed RED scenarios.

## What to do

1. Panic-mode gate (CONDITIONAL — if < 30 min to deadline AND unexpected RED): triage failing specs. Cut `@extended` first via `test.skip(true, 'panic-mode-time-cut')`, then `@boundary`. A `@critical` spec failing for an unfixable reason → `test.skip(true, 'bug-NNN — known app defect')`. Re-run filtered: `npx playwright test --grep '@smoke|@critical'`.
2. Security audit: scan for secret commits via `git log -p | grep -iE 'password|api[_-]?key|secret'`, confirm `.env` not committed, confirm `playwright-report/` and `test-results/` are gitignored.
3. Test-coverage cross-check: confirm `@smoke @critical @regression @negative @boundary @security` tag families across `tests/**/*.spec.ts` are all populated, `bugs/BUG-*.md` count matches `@known-bug` spec count, `bugs/README.md` index totals match file count. Regenerate via `./scripts/new-bug.sh --reindex` if drift.
4. Doc sanity: verify `solution/ARCHITECTURE.md`, `solution/README.md`, `bugs/README.md`, `reports/summary.md` cross-reference. `./run-tests.sh --help` works.
5. Run triager first-check if `reports/last-run.json` shows ANY unmatched failure. STOP if user defers triage decision.
6. Final commit: `git status` then explicit `git add <files> && git commit -m 'chore: finalize submission'`.
7. ZIP (if required): explicit allowlist via `zip -r submission.zip tests/ package.json package-lock.json playwright.config.ts run-tests.sh bugs/ reports/ evidence/ STATUS.md -x 'node_modules/*' 'playwright-report/*' 'test-results/*'`. Verify with `unzip -l submission.zip | head -30`.
8. Trigger reviewer final-gate (BLOCKING — produces `reports/reviews/final-gate.md` with verdict GO | GO-WITH-RISK | NO-GO). DO NOT submit until verdict received.
9. Seal STATUS.md: append `## Submission` section with final-gate verdict, ready-at timestamp, submitted-at placeholder. Final commit `chore: seal STATUS for submission` if changes.

## Output

- Final commit `chore: finalize submission`.
- Optional `submission.zip` (explicit allowlist; typically < 10 MB).
- `STATUS.md` sealed with `SUBMITTED-AT` (or `READY-AT` if user submits manually).
- Confirmation that `reports/reviews/final-gate.md` was written by reviewer with GO / GO-WITH-RISK verdict.

## Example

The final-commit step plus the report + trace wiring it confirms:

```bash
git add tests/ package.json package-lock.json playwright.config.ts STATUS.md
git commit -m "chore: finalize submission"

# playwright.config.ts — HTML report + trace kept on failure
# reporter: [['html', { outputFolder: 'playwright-report' }]],
# use: { trace: 'retain-on-failure' },
```
