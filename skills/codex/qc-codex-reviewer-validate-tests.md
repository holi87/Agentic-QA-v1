---
name: qc-codex-reviewer-validate-tests
description: "QA Lead reviewer auditing generated Playwright + TypeScript test quality. Independent audit of assertion strength (BIZ vs TECH split), bug-aware policy adherence, anti-patterns, isolation/cleanup discipline, and traceability to requirements."
---

# Skill: qc-codex-reviewer-validate-tests

## Communication

${include_preamble}

## Standards

This skill operates under shared conventions — read the canonical source, do not restate it:

- Generated-code structure & idioms (Playwright + TypeScript) — `docs/standards/playwright-ts-standards.md` (§1 layering, §2 Page Objects, §3 typed clients, §4 fixtures, §5 assertions/waiting, §6 size limits, §8 security).
- BIZ/TECH assertion descriptors — `docs/standards/biz-tech-assertions.md`.
- Bug-aware policy (keep failing assertions, file a bug) — `docs/bug-aware-policy.md` §2, §6.

## When to use

- After implementer implement-api or implement-ui slice + verify.
- After a major implementation milestone (per-slice or per-area).
- NOT during implementation (review is single-shot per slice).

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- no spec sources exist under `tests/` (no `*.spec.ts`) → `needs_input: tests`.
- `reports/last-run.json` is missing (cannot confirm failures are classified) → `needs_input: test_run`.
- a scenario tagged `@known-bug @bug-NNN` has no matching `bugs/BUG-NNN-*.md` → `needs_input: bug_file`.

## What to do

1. Read inputs: `tests/**/*.spec.ts`, Page Objects + API clients (`tests/**/*-page.ts`, `tests/**/*-api-client.ts`), `tests/**/schemas/*.json`, `tests/fixtures.ts`, `IMPLEMENTATION_PROGRESS.md`, `bugs/`, `requirements.md`, standards (`playwright-ts-standards.md`, `biz-tech-assertions.md`), bug-reporting.md.
2. Assertion strength: web-first assertions (`expect(locator).toBeVisible()` / `toHaveText()` / `toHaveURL()`)? Status + body-shape checks on API specs? `expect.soft(...)` for BIZ/TECH companions with leading `// BIZ:` / `// TECH:` comments? At least 1 BIZ assertion per `@critical` scenario? Assertions reference Business Invariants from requirements.md?
3. Bug-aware policy violations: assertion adjusted to mask app behavior? Weak `toContain(...)`, broad `toBeTruthy()` / `not.toBeNull()`, status accept-list (`expect([200, 400]).toContain(res.status())`)? Commented-out assertion ('// fix later')? `@known-bug @bug-NNN` without a matching `bugs/BUG-NNN-<slug>.md`?
4. Anti-patterns: a hard wait (`page.waitForTimeout`) anywhere? `console.log` of a token / auth header / secret-bearing body? Hard-coded base URL or credentials instead of `process.env`? String-concatenated URLs from untrusted input? `try/catch` swallowing a failure? `any` types?
5. Spec discipline: specs orchestrate + assert only (tests typically ≤ 40 lines, §6)? Selectors and `request` plumbing hidden behind Page Objects / clients? Dependency injection via `test.extend` fixtures (no module-level mutable state, no singletons)?
6. API client quality: one typed client per resource wrapping `APIRequestContext`? Request/response bodies are TypeScript `interface`s (no `any`)? Base URL + auth read from the environment?
7. Page Object quality: locators `getByRole` / `getByLabel` / `getByText` / `getByTestId` over CSS / XPath? No hard wait? `readonly` locator fields? One Page Object per page (no god-object)?
8. Isolation & lifecycle: per-test fixtures around `use()`? Fresh browser context per test (no cross-test sharing)? Cleanup of created resources in teardown? Screenshot / trace captured on failure only?
9. Test data strategy: factory / builder helpers (non-production fixtures)? Cleanup tracks created IDs? No shared mutable state across tests?
10. Size & structure: file ≤ 300 lines, function ≤ 40 lines, nesting depth ≤ 3 (§6, the C2 lint gate)? Traces / HAR treated as sensitive (no secret echoed in assertions or logs)?
11. Traceability: each Top 5 path has ≥ 1 implemented `@critical` scenario? Business Assertions Matrix invariants verifiable in code?
12. Coverage depth gate (issue #233 — quantitative marker contract): grep each generated `*.spec.ts` for the fixed marker comments emitted by the UI generator (#230) and API generator (#231). Do NOT read test logic for this check — only count markers.

   **UI spec — required markers (when `autonomy.coverage_floor=true`):**
   - `agentic-os:floor:console` — console + pageerror listener.
   - `agentic-os:floor:network` — requestfailed + 5xx response capture.
   - `agentic-os:floor:a11y` — axe-core scan block (fail-soft when dep absent).
   - `agentic-os:floor:link-walk` — link-integrity check (skip for form targets: `/new`, `/create`, `/edit`, `/login`, `/signup`, `/register`).

   **API spec — required companion markers (when `autonomy.coverage_floor=true`):**
   - `agentic-os:companion:neg-auth` — required when `credentials_env` is set in the candidate.
   - `agentic-os:companion:boundary` — required when method ∈ {POST, PUT, PATCH}.
   - `agentic-os:companion:schema` — always required (the generator emits it unconditionally; absence implies generator regression).
   - `agentic-os:companion:bola` / `injection` — optional reinforcements; counted but not blocking.

   **Hard items (always required, independent of the flag):**
   - UI: ≥ 1 of `toHaveURL` / `getByRole` / `getByText` / `getByLabel`.
   - API: ≥ 1 `expect(response.status()).toBe(...)`.

   **Verdict mapping:**
   | Condition | Verdict | Reason code |
   |---|---|---|
   | Hard items present + all required floor markers present | PASS | — |
   | Hard items present + ≥ 1 floor marker missing + `autonomy.coverage_floor=true` | REJECT | `coverage_floor_missing` |
   | Hard items present + ≥ 1 floor marker missing + `autonomy.coverage_floor=false` | PASS with WARN | `coverage_floor_missing` |
   | Hard items missing | REJECT | `business_assertion_missing` |

   **Programmatic helper:** `agentic_os.coverage_review.evaluate_ui_coverage` and `evaluate_api_coverage` implement this exact table — call them from review automation when one is available.

   Soft-asserts in companion blocks NEVER mask the primary plan-derived assertion (Hard rule #2 preserved).

## Output

- `reports/reviews/tests.md` (append section if existing) with:
  - Verdict: PASS | PASS-WITH-CHANGES | FAIL.
  - Bug-Aware Policy Violations (file:line + 1-line diff suggestion).
  - Anti-Patterns table.
  - Assertion Quality table (per scenario: BIZ count / TECH count / soft? / Issues).
  - Spec / Page Object / Client Discipline findings.
  - Cleanup Audit findings.
  - Traceability table (Top-5 path ↔ implemented `@critical`).
  - Recommended Next Action.

## Example

A bug-aware violation finding + assertion-quality row in `reports/reviews/tests.md`. Descriptor convention: `docs/standards/biz-tech-assertions.md`:

```markdown
### Bug-Aware Policy Violations
- `tests/api/orders.spec.ts:42` — assertion weakened to `expect([200, 409]).toContain(res.status())` to mask a failing cancel. Restore `// BIZ: cancel is refused` + `expect.soft(res.status(), 'BIZ: cancel is refused').toBe(409)` and file a bug instead.

### Assertion Quality
| Scenario | BIZ | TECH | Soft? | Issue |
|---|---|---|---|---|
| Cancelling a paid order is refused | 1 | 1 | yes | — |
```
