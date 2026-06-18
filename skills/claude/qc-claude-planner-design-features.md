---
name: qc-claude-planner-design-features
description: "Design Playwright Test spec skeletons (TypeScript) under tests/<area>.spec.ts for all top-5 critical user goals plus negative/boundary/security scenarios. Skeletons only — spec bodies + typed clients/page objects implemented separately."
---

# Skill: qc-claude-planner-design-features

## Communication

${include_preamble}

## Standards

This skill operates under shared conventions — read the canonical source, do not restate it:

- Generated-code structure & idioms (Playwright + TypeScript) — `docs/standards/playwright-ts-standards.md` (§1 layering / specs orchestrate, §2 page objects, §5 web-first assertions + no hard waits, §7 tags).
- Coverage targets, ISO 25010 dimensions, OWASP API Top 10, severity & traceability — `docs/standards/qa-standards.md`.

## When to use

- T+01:30, after explore-sut.
- requirements.md complete with business invariants + tag plan.
- BEFORE implementer spec bodies / typed clients / page objects exist.
- NOT if requirements.md incomplete (return to analyze first).

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- `requirements.md` is missing or lists no top-5 critical paths → `needs_input: requirements`.
- there are no approved candidates / TEST-PLAN entries to design against → `needs_input: candidate_metadata`.
- business-area tags cannot be inferred from the brief → `needs_input: areas`.

## What to do

1. Read requirements.md top-5 + Business Assertions Matrix.
2. Group by area — one `tests/<area>.spec.ts` per business area (auth, users-crud, orders, security, etc.).
3. Open each file with a `test.describe('<area>', ...)` block tying to its ISO 25010 dimension.
4. Use a `test.beforeEach` (or a shared fixture from `tests/fixtures.ts`) only when ≥ 3 tests in the file share the setup.
5. Tests per layer — one `test(...)` per scenario, tags via the `{ tag: [...] }` arg:
   - Happy path (`{ tag: ['@functional-<area>', '@critical', '@smoke', '@regression'] }`): 1-2 per area.
   - Negative (`{ tag: ['@negative', '@regression'] }`): 1 per documented error code.
   - Boundary (`{ tag: ['@boundary', '@regression'] }`): 1-2 per numeric/string/date field.
   - Security (`{ tag: ['@security', '@regression', '@owasp-apiN'] }`): per applicable OWASP item.
6. For parametric variants (boundary tables, equivalence classes), loop a typed data table and emit one `test(...)` per case so each row reports independently.
7. Coverage floor: every non-trivial task needs more than a single smoke path. For public web tasks include homepage, navigation, representative detail page, feed/RSS or sitemap/robots where present, broken image check, basic accessibility, and console-error observation unless explicitly out of scope.
8. Specs orchestrate and assert only — name intent, not transport. NO inline `request.post('/users')` in the spec body — the body calls a typed client / page object (`await usersApi.create(validUser)`); design the skeleton so the implementer wires that, not raw HTTP.
9. Tag policy: every test has `@functional-<area>` + one of `@smoke @critical @regression` + `@negative @boundary @security @extended` as applicable + `@known-bug @bug-NNN` if pinned to a logged bug.
10. Map tag buckets to selection lanes (no separate runner files): `npx playwright test --grep @smoke`, `--grep @critical`, `--grep @security`, full run = no grep. Encode lanes as `playwright.config.ts` projects only if the suite needs distinct config per lane.
11. Verify discovery without running bodies: `npx playwright test --list` (skeleton/`test.fixme` bodies OK).
12. Commit: `git add tests/ && git commit -m 'feat: design Playwright spec skeletons'`.

## Output

- `tests/<area>.spec.ts` — one per business area; `test.describe` + `test(...)` skeletons with the canonical tag shape.
- Selection lanes documented as `--grep` invocations (and `playwright.config.ts` projects only when per-lane config differs).
- `STATUS.md` — design summary.
- Git commit `feat: design Playwright spec skeletons`.

## Example

A `tests/<area>.spec.ts` skeleton with the canonical tag shape + shared setup (`test.describe` + `test(...)`, bodies wired by the implementer):

```typescript
import { test, expect } from './fixtures';

test.describe('orders', () => {
  test.beforeEach(async ({ ordersApi }) => {
    await ordersApi.seedPaidOrder();
  });

  test('cancelling a paid order is refused',
    { tag: ['@functional-orders', '@critical', '@smoke'] },
    async ({ ordersApi }) => {
      const res = await ordersApi.cancel(orderId);
      expect(res.status()).toBe(409);                 // cancel refused on a paid order
      expect((await res.json()).balance).toBe(opening); // balance unchanged
    });
});
```
