---
name: qc-codex-implementer-implement-api
description: "Implement Playwright Test API specs in TypeScript using a typed APIRequestContext client with status + schema + web-first assertions, strict business vs technical assertion separation, env-injected base URLs, and bug-aware policy."
---

# Skill: qc-codex-implementer-implement-api

## Communication

${include_preamble}

## Standards

This skill operates under shared conventions — read the canonical source, do not restate it:

- Generated-code structure & idioms (Playwright + TypeScript) — `docs/standards/playwright-ts-standards.md` (§1 layering, §3 typed clients, §4 fixtures, §5 assertions/waiting, §6 size limits, §8 security).
- BIZ/TECH assertion descriptors — `docs/standards/biz-tech-assertions.md`.
- Cucumber tag families (carried over to Playwright `{ tag: [...] }`) — `docs/standards/cucumber-tags.md`.
- Bug-aware policy (keep failing assertions, file a bug) — `docs/bug-aware-policy.md` §2, §6.

## When to use

- T+02:00 — first API implementation pass.
- T+05:30 — Round 2 (@boundary @extended @security deep).
- After design-features produced scenario candidates with @functional-api-* tags.
- NOT for UI-only features (use implement-ui).

## What to do

1. Pick a slice: one approved candidate / spec file / tag bucket per slice; slice = atomic commit. Respect configured surfaces: if `sut.api.enabled=false` or the task says UI-only, do not implement heuristic API tests from prose routes.
   - Coverage source of truth: implement only `generate_now` candidates or scenarios explicitly approved in requirements/TEST-PLAN. If no candidate has exact assertion + data + cleanup, STOP with `needs_input: candidate_metadata`.
2. Map each approved candidate to a spec under `tests/api/<area>.spec.ts`; group by resource/area, one `test(...)` per scenario.
3. Build / extend a typed API client (one client per resource group, e.g. `UserApiClient` in `tests/api/<area>-api-client.ts`) wrapping `APIRequestContext`. Request/response bodies are TypeScript `interface`s — never `any` (§3). The base URL and auth come from the environment: read `process.env['API_BASE_URL']` and build the auth header from an env-named token, never an inline secret value (§8).
4. Inject the client into specs as a fixture (`test.extend` in `tests/fixtures.ts`) — no module-level mutable state, no `request` plumbing in the spec body (§1, §4).
5. Specs orchestrate and assert only: call the client, assert on the result. A `*.spec.ts` reads like the test plan.
6. Assertions — CRITICAL:
   - Status + body shape: `expect(res.status()).toBe(201)` / `expect(res.ok()).toBeTruthy()`; validate the body against a JSON schema when one is available.
   - BIZ vs TECH split per `docs/standards/biz-tech-assertions.md`: a leading `// BIZ:` / `// TECH:` comment plus `expect.soft(value, 'BIZ: ...')` so a soft companion never masks the primary assertion.
   - Never weaken: no status accept-lists (`[200, 400]`), no broad `toBeTruthy()` where a value check is owed.
7. Evidence: the run captures the Playwright trace + HTML report on failure (config already wired) — never `console.log` a token, an auth header, or a secret-bearing body (§8).
8. Setup/teardown: per-test fixtures around `use()`; clean up resources the test created in the fixture teardown. Each test is isolated.
9. Coverage breadth: do not accept only happy-path status checks; include documented negative, boundary, schema/body, auth, and business-invariant assertions where they apply. When `autonomy.coverage_floor=true`, emit the API companion markers (`agentic-os:companion:neg-auth|boundary|schema`).
10. Bug-aware: if an assertion fails AND the spec says it is correct → log `bugs/BUG-NNN-<slug>.md`, tag the scenario `@known-bug @bug-NNN`, KEEP the assertion. NEVER weaken thresholds to mask app bugs.
11. Tags: Playwright tag syntax `{ tag: ['@functional-<area>', '@smoke'] }` — one `@functional-<area>` + at least one lifecycle tag (`docs/standards/cucumber-tags.md`).
12. Quick local verify: `npx playwright test --grep @functional-<area>` then `npx tsc --noEmit` on the touched scope.
13. Commit per slice: `git add ... && git commit -m 'feat: implement <area> API tests'`.

## Output

- `tests/api/<area>-api-client.ts` — typed `APIRequestContext` client.
- `tests/api/<area>.spec.ts` — Playwright Test specs (orchestrate + assert).
- `tests/fixtures.ts` — extended with the client for dependency injection.
- Optional `tests/api/schemas/<area>.json` consumed by the body-shape assertion.
- `IMPLEMENTATION_PROGRESS.md` updated.
- Git commit `feat: implement <area> API tests` per slice.

## Example

A Playwright Test API spec with a typed client fixture and a BIZ/TECH soft-assertion split (convention: `docs/standards/biz-tech-assertions.md`):

```typescript
test('a refused cancel leaves the balance intact',
  { tag: ['@functional-orders', '@regression'] },
  async ({ ordersApi }) => {
    const res = await ordersApi.cancel(orderId);
    // TECH: cancel on a paid order returns 409
    expect.soft(res.status(), 'TECH: cancel on a paid order returns 409').toBe(409);
    const body = await res.json();
    // BIZ: the account balance is unchanged after a refused cancel
    expect.soft(body.balance, 'BIZ: balance unchanged after a refused cancel')
      .toBe(expectedBalance);
  });
```
