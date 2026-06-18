---
name: qc-gemini-reviewer-validate-features
description: "Test-design coach + ISTQB reviewer. Independent audit of `.spec.ts` structure + Playwright projects/tag selection. Catches describe/test naming smell, missing test-design technique coverage, tag discipline drift, project/grep mis-config BEFORE implementation wastes hours."
---

# Skill: qc-gemini-reviewer-validate-features

## Communication

${include_preamble}

## Standards

This skill operates under shared conventions — read the canonical source, do not restate it:

- Generated-code structure & idioms (Playwright + TypeScript) — `docs/standards/playwright-ts-standards.md` (§1 layering, §2 Page Object Model, §5 assertions/web-first waiting, §6 size limits, §7 tags, §8 security).
- Tag families (carried over to Playwright `{ tag: [...] }` + `--grep`) — `docs/standards/cucumber-tags.md`.
- Coverage targets, severity, traceability, OWASP API Top 10, WCAG 2.2 — `docs/standards/qa-standards.md`.
- Bug-aware policy (a known app bug is `@known-bug @bug-NNN`, not a deleted test) — `docs/bug-aware-policy.md`.

## When to use

- After planner design-features produced `tests/<area>.spec.ts` skeletons (`test.describe` + `test(...)` titles, no bodies yet).
- BEFORE implementer fills spec bodies with typed clients / page objects.
- NOT mid-implementation (review pass is single-shot per design iteration).

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- no `*.spec.ts` files exist under `tests/` → `needs_input: features`.
- `requirements.md` is missing (coverage cannot be mapped to paths) → `needs_input: requirements`.
- no `playwright.config.ts` with project + tag selection to validate against → `needs_input: projects`.

## What to do

1. Read inputs: `tests/**/*.spec.ts`, `playwright.config.ts`, `package.json` scripts, `requirements.md`, `bugs/`, standards files (§1, §2, §5, §7).
2. Audit implementation leakage in spec bodies/titles: a `test(...)` calling `page.locator('#id')`, `request.post(...)`, raw selectors, status codes or JSON paths inline → red flag (belongs in a page object / typed client per §1–§2). Suggest 1-line rewrite per smell.
3. Audit test title quality: `test.describe('<area/feature>')` groups one concept; each `test('<observable outcome from the user's view>')` reads like a test-plan line, not an implementation step.
4. Audit reusability: same intent expressed 3 ways across specs → extract a shared page object / client method, do not re-inline.
5. Audit tag discipline: every `test(...)` has `{ tag: [...] }` with `@functional-<area>` + one of `@smoke @critical @regression` + `@negative` per documented error code + `@boundary` for numeric/string/date edges + `@security` mapped to OWASP API Top 10 + `@known-bug @bug-NNN` matching existing `bugs/` files. Lowercase + hyphenated (§7).
6. Coverage vs Top 5 paths: each path has ≥ 1 `@critical` test? Smoke set 3-5 (not 50)?
7. Public web coverage floor: include navigation, representative detail pages, feed/sitemap/robots where applicable, broken assets, accessibility basics, and console-error observation unless explicitly out of scope.
8. Coverage vs error codes: each documented code → at least one `@negative` test?
9. BVA: numeric/string/date inputs in top-5 paths covered by boundary tests?
10. Equivalence partitioning expressed as data-driven tests (`for (const row of cases) test(...)` or a parameterized `test.describe`)? Multi-conditional logic covered by a decision-table data set?
11. Shared setup: preconditions for ≥ 3 tests live in `beforeEach`/a fixture, not copy-pasted into each `test(...)`?
12. OWASP API Top 10 coverage matrix per applicable item.
13. WCAG / a11y: if UI features present, at least one `@security-a11y` test?
14. Selection correctness: `playwright.config.ts` projects + `npm`/`npx` scripts (`npx playwright test --grep @smoke`) select the intended tag sets; no orphan tag with no project/grep that targets it.
15. Web-first assertion readiness: titles imply `await expect(locator).toBeVisible()` / status + body-shape checks (§5); flag any title that can only be met with a hard wait (`page.waitForTimeout`) → red flag.

## Output

- `reports/reviews/features.md` with:
  - Verdict: PASS | PASS-WITH-CHANGES | FAIL.
  - Tag Coverage Matrix table.
  - Coverage vs Top 5 Critical Paths table.
  - OWASP API Top 10 Coverage table.
  - Critical / Strong / Suggested findings with `file:line` references + 1-line rewrite.
  - Recommended Next Action (one-liner).

## Example

Findings rows in `reports/reviews/features.md` (one line per smell, `file:line` + 1-line rewrite):

```markdown
### Critical findings
- `tests/orders.spec.ts:14` — implementation leakage: spec body calls `request.post('/api/orders/1/cancel')`. Rewrite: move to `ordersApi.cancel(orderId)` (typed client, §1).
- `tests/orders.spec.ts:22` — test missing the `@functional-orders` tag.

Verdict: PASS-WITH-CHANGES
```
