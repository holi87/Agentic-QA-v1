---
name: qc-codex-reviewer-final-gate
description: "FINAL independent gate before submission. Verifies, scores risk, decides GO / GO-WITH-RISK / NO-GO. BLOCKING — user MUST NOT submit without this verdict."
---

# Skill: qc-codex-reviewer-final-gate

## Communication

${include_preamble}

## When to use

- After implementer package step.
- Last gate before submission. BLOCKING.
- NOT mid-implementation; NOT before package.

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- `solution/ARCHITECTURE.md` is missing or still has TBD sections → `needs_input: architecture`.
- `reports/last-run.json` is missing or older than the latest implementation commit → `needs_input: test_run`.
- no `reports/reviews/*.md` exist from the prior validate gates → `needs_input: prior_reviews`.
- the contest deliverable set cannot be resolved at repo root → `needs_input: deliverables`.

## What to do

1. Audit Architecture (30% weight): delegate the deep audit to `reviewer-validate-architecture` (runtime/CLI-contract invariants) and fold its verdict in here. Confirm `solution/ARCHITECTURE.md` has all 7 required sections filled (no TBD), Section 5 (AI Workflow) narrates AI tools + skills used, SOLID, clear API/UI/support separation, fixture-based DI (Playwright `test.extend`) cleanly applied.
2. Audit Tests (30% weight): grep the top 5 critical `tests/**/*.spec.ts` for the `@critical` tag. Confirm the BIZ + TECH assertion split uses `expect.soft` per test (`docs/standards/playwright-ts-standards.md` §5). Open OWASP API coverage matrix — every row populated. Run final `npx playwright test` — every failure classified.
3. Audit Code Quality (20% weight): grep for hard waits (`page.waitForTimeout`, fixed `setTimeout`), `console.log`, hardcoded credentials / URLs — must be zero (`docs/standards/playwright-ts-standards.md` §5, §8). Confirm request/response DTOs are TypeScript `interface`s — never `any` — and dependency versions carry no known critical CVE (cross-check `package-lock.json` via `npm audit` against NVD/GHSA).
4. Audit Documentation (20% weight): list contest deliverables at root (`solution/ARCHITECTURE.md`, `solution/README.md`, `bugs/`, `reports/`, `run-tests.sh`, `tests/`). Execute `solution/README.md` quick-start verbatim — must run. Compare `bugs/*.md` file count to unique `@bug-NNN` tag count — equal. Verify `bugs/README.md` index sorted by severity desc.
5. Audit Hygiene: `git log -p` filtered for secret patterns — zero hits. `.gitignore` excludes build artifacts + `playwright-report/` + `test-results/`. If ZIP — extract, verify contents, size < 10 MB.
6. Apply decision matrix (see Output) → verdict.
7. Write `reports/reviews/final-gate.md` populated per Output spec.
8. Commit: `git add reports/reviews/final-gate.md STATUS.md && git commit -m 'docs: final gate verdict'`.

## Output

- `reports/reviews/final-gate.md` with:
  - Verdict line: GO | GO-WITH-RISK | NO-GO.
  - Score Breakdown table (qualitative per dimension with 2-sentence justification).
  - Critical Blockers section (NO-GO triggers).
  - Documented Risks section (GO-WITH-RISK: description / impact / mitigation).
  - Top Strengths (3, lead with these).
  - Top Weaknesses (2, stakeholders may probe).
  - Submission Recommendation.
  - If NO-GO — remediation plan with owner + time estimate per step.
- `STATUS.md` row appended: `T+NN:NN final-gate <verdict>`.
- Git commit `docs: final gate verdict`.

### Decision matrix

| Verdict | Required conditions |
|---|---|
| GO | No Critical findings AND every rubric dimension scored PASS (no Critical or High finding in it) AND all docs cross-referenced |
| GO-WITH-RISK | ≤ 2 documented risks AND mitigation logged in `STATUS.md` AND risks do NOT touch security or architecture fundamentals |
| NO-GO | ANY of: hardcoded secret in submitted code / zero IDOR coverage / unclassified failing tests / documentation contradicts code / missing contest deliverable |

## Example

The verdict block written to `reports/reviews/final-gate.md`:

```markdown
verdict: GO-WITH-RISK
reason: documented-risk-accepted
findings:
  - API5 BFLA coverage partial — 1 documented risk, mitigation logged in STATUS.md
score: { architecture: strong, tests: strong, code: pass, documentation: strong }
READY
```
