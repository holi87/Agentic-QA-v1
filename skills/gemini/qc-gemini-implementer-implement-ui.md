---
name: qc-gemini-implementer-implement-ui
description: "Implement Playwright Test UI specs in TypeScript using the Page Object Model with role/label/text locators, web-first assertions, fixtures for dependency injection, no hard waits, strict business vs technical assertion separation, and bug-aware policy."
---

# Skill: qc-gemini-implementer-implement-ui

## Communication

${include_preamble}

## Standards

This skill operates under shared conventions — read the canonical source, do not restate it:

- Generated-code structure & idioms (Playwright + TypeScript) — `docs/standards/playwright-ts-standards.md` (§1 layering, §2 Page Objects, §4 fixtures, §5 assertions/waiting, §6 size limits, §8 security).
- BIZ/TECH assertion descriptors — `docs/standards/biz-tech-assertions.md`.
- Cucumber tag families (carried over to Playwright `{ tag: [...] }`) — `docs/standards/cucumber-tags.md`.
- Bug-aware policy (keep failing assertions, file a bug) — `docs/bug-aware-policy.md` §2, §6.

## When to use

- T+03:40 — after first API implementation slice + verify.
- UI base URL accessible.
- Approved candidates carry `@functional-ui-*` scenarios.
- NOT for API-only tasks (use implement-api).

## What to do

1. Read the approved UI candidates for the area and map each to a spec under `tests/ui/<area>.spec.ts`. Implement only `generate_now` candidates or explicitly approved UI scenarios. If the target page, the visible assertion, the data, or the cleanup is missing, STOP with `needs_input: candidate_metadata`.
2. Page Object per page/area (`<Area>Page` in `tests/ui/<area>-page.ts`). The constructor takes the Playwright `Page`; locators are `readonly` fields; methods express user intent. Keep assertions in the spec — a Page Object models the page, it is not the test (§2).
3. Locator strategy: `getByRole` > `getByLabel` > `getByText` > `getByTestId` > CSS > XPath. NEVER use indexed CSS like `nth-child`. If a locator matches multiple elements, refine to role/name or a scoped locator; do not weaken the assertion.
4. NEVER use a hard wait — `page.waitForTimeout(...)` is forbidden (§5); web-first assertions auto-wait, and `locator.waitFor()` covers explicit state synchronization. The C2 lint gate rejects hard waits statically.
5. Inject Page Objects into specs as fixtures (`test.extend` in `tests/fixtures.ts`); specs hold no raw `page` calls and no selectors — they call the Page Object and assert (§1, §4).
6. Isolation: each test runs in a fresh browser context (Playwright default); per-test setup/teardown lives in the fixture around `use()`; no module-level mutable state.
7. Assertions: web-first `await expect(locator).toBeVisible()` / `toHaveText()` / `toHaveURL()`. BIZ vs TECH split per `docs/standards/biz-tech-assertions.md`: a leading `// BIZ:` / `// TECH:` comment plus `expect.soft(...)` for companions so a soft check never masks the primary assertion.
8. Coverage breadth: for exploratory public web UI implement more than a homepage smoke unless scope says otherwise — navigation, detail page, link integrity, asset health, console errors, and accessibility basics. When `autonomy.coverage_floor=true`, emit the UI floor markers (`agentic-os:floor:console|network|a11y|link-walk`).
9. Evidence: capture a screenshot + trace on failure (config already wired); treat traces/HAR as sensitive — they record the `Authorization` header — and never `console.log` a secret value (§8).
10. A11y smoke (conditional): when WCAG matters per requirements.md add a `@security-a11y` scenario via `@axe-core/playwright` (optional dynamic import, fail-soft when the dep is absent).
11. Bug-aware: a visual mismatch with the spec → log a bug, KEEP the assertion. An accessibility violation → log it mapped to the WCAG criterion (e.g. WCAG 2.2 SC 1.1.1).
12. Tags: Playwright tag syntax `{ tag: ['@functional-ui-<area>', '@smoke'] }` — one `@functional-ui-<area>` + at least one lifecycle tag (`docs/standards/cucumber-tags.md`).
13. Quick local verify: `npx playwright test --grep @functional-ui-<area>` then `npx tsc --noEmit` on the touched scope.
14. Commit per slice: `git add ... && git commit -m 'feat: implement <area> UI tests'`.

## Output

- `tests/ui/<area>-page.ts` — one Page Object per page.
- `tests/ui/<area>.spec.ts` — Playwright Test specs (orchestrate + assert).
- `tests/fixtures.ts` — extended with Page Objects for dependency injection.
- Optional `tests/ui/a11y.ts` — `@axe-core/playwright` helper.
- `IMPLEMENTATION_PROGRESS.md` updated.
- Git commit `feat: implement <area> UI tests` per slice.

## Example

A Page Object + Playwright Test spec using role/test-id locators (no hard waits, BIZ/TECH descriptors per `docs/standards/biz-tech-assertions.md`):

```typescript
// orders-page.ts
import { type Page, type Locator } from '@playwright/test';

export class OrdersPage {
  private readonly banner: Locator;

  constructor(private readonly page: Page) {
    this.banner = page.getByRole('alert');
  }

  async cancel(orderId: string): Promise<void> {
    await this.page
      .getByTestId(`order-${orderId}`)
      .getByRole('button', { name: 'Cancel' })
      .click();
  }

  bannerLocator(): Locator {
    return this.banner;
  }
}

// orders.spec.ts
test('a refused cancel shows the paid notice',
  { tag: ['@functional-ui-orders', '@smoke'] },
  async ({ ordersPage }) => {
    await ordersPage.cancel(orderId);
    // BIZ: a refused cancel shows the 'order is paid' notice
    await expect(ordersPage.bannerLocator(), 'BIZ: refused cancel shows the paid notice')
      .toBeVisible();
  });
```
