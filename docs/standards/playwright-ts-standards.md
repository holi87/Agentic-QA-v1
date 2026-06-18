# Coding Standards — Playwright + TypeScript

Status: active

This file = HOW we write generated test code. Sister doc `qa-standards.md` =
WHAT quality bar (ISO 25010, ISO/IEC/IEEE 29119, ISTQB, OWASP API Top 10,
WCAG 2.2). Always read both. This doc prescribes structure, names and idioms;
`qa-standards.md` prescribes coverage targets, severity, traceability. On
conflict — qa-standards wins on intent, this doc wins on syntax.

Canonical stack per
`ADR-0002`:
**`@playwright/test` (TypeScript)** — UI via Playwright Test, API via
`request` / `APIRequestContext`, optional DB via a typed client in fixtures.
Node.js LTS, strict TypeScript (`tsc --noEmit`), ESLint. (Supersedes the
inherited Java BDD `coding-standards.md`.)

## 1. Layering & Single Responsibility

- **One page / area = one Page Object** (`LoginPage`, `CartPage`). **One API
  resource = one typed client** (`UserApiClient`).
- **Spec files orchestrate and assert only** — they call Page Objects and API
  clients; no selectors, no `fetch`/`request` plumbing, no business logic
  inline. A `*.spec.ts` reads like the test plan.
- **Dependency Inversion** — specs depend on the abstractions (Page Objects,
  API clients) injected as fixtures, never on raw `page` / `request` directly.
- One top-level concept per file.

## 2. Page Object Model

A Page Object wraps one page/area: locators as `readonly` fields built with
**role/label/text** locators (resilient, accessibility-first), and methods
that express user intent. Keep assertions in the spec (or in thin, explicitly
named expectation helpers) — a Page Object models the page, it is not the test.

```typescript
// GOOD — login-page.ts
import { type Page, type Locator } from '@playwright/test';

export class LoginPage {
  private readonly username: Locator;
  private readonly password: Locator;
  private readonly submit: Locator;

  constructor(private readonly page: Page) {
    this.username = page.getByLabel('Username');
    this.password = page.getByLabel('Password');
    this.submit = page.getByRole('button', { name: 'Sign in' });
  }

  async goto(): Promise<void> {
    await this.page.goto('/login');
  }

  async signIn(user: string, pass: string): Promise<void> {
    await this.username.fill(user);
    await this.password.fill(pass);
    await this.submit.click();
  }
}
```

```typescript
// BAD — selectors + business logic leaking into the spec
test('login', async ({ page }) => {
  await page.goto('/login');
  await page.locator('#u').fill('a');          // raw CSS selector
  await page.locator('#p').fill('b');
  await page.locator('button.primary').click();
  await page.waitForTimeout(2000);             // hard wait (see §5)
});
```

## 3. Typed API clients

Wrap `APIRequestContext` in a typed client. Request/response bodies are
TypeScript `interface`s — no `any`. The base URL and auth come from config /
env (never hard-coded, never an inline secret — see
[`docs/docker-networking-contract.md`](../docker-networking-contract.md)).

```typescript
// GOOD — user-api-client.ts
import { type APIRequestContext, type APIResponse } from '@playwright/test';

export interface User { id: number; email: string; }

export class UserApiClient {
  constructor(private readonly request: APIRequestContext) {}

  async create(email: string): Promise<APIResponse> {
    return this.request.post('/users', { data: { email } });
  }

  async get(id: number): Promise<User> {
    const res = await this.request.get(`/users/${id}`);
    return (await res.json()) as User;
  }
}
```

## 4. Fixtures as dependency injection

Inject Page Objects, API clients and test data with `test.extend`. No
module-level mutable state, no singletons, no cross-test sharing — each test
is isolated (Playwright gives every test a fresh browser context). Per-test
setup/teardown lives in the fixture around `use()`.

```typescript
// GOOD — fixtures.ts
import { test as base } from '@playwright/test';
import { LoginPage } from './login-page';
import { UserApiClient } from './user-api-client';

export const test = base.extend<{
  loginPage: LoginPage;
  userApi: UserApiClient;
}>({
  loginPage: async ({ page }, use) => { await use(new LoginPage(page)); },
  userApi: async ({ request }, use) => { await use(new UserApiClient(request)); },
});
export { expect } from '@playwright/test';
```

```typescript
// BAD — shared global state across tests (flaky, order-dependent)
let cachedUser: User;                          // module-level mutable state
test('a', async ({ userApi }) => { cachedUser = await userApi.get(1); });
test('b', async () => { expect(cachedUser.email).toBe('x'); }); // depends on 'a'
```

## 5. Assertions & waiting

- **Web-first assertions only** — `await expect(locator).toBeVisible()` /
  `toHaveText()` auto-wait and retry. Never assert on a stale snapshot.
- **No hard waits** — `page.waitForTimeout(...)` is forbidden; wait on the
  condition (locator state, response, URL), not the clock.
- **API** — assert the status (`expect(res.ok()).toBeTruthy()` /
  `expect(res.status()).toBe(201)`) and the body shape; validate against the
  JSON schema when one is available.

## 6. Size & structure limits (enforced by the C2 lint ruleset)

- Spec / Page Object / client file **≤ 300 lines**; function **≤ 40 lines**;
  nesting depth **≤ 3**.
- No `any`; `strict` TypeScript; no unused exports.
- These limits are enforced statically by the generated framework's ESLint +
  `tsc --noEmit` ruleset (the "C2" lint gate); a file over budget is a
  decomposition signal — split the Page Object / client.

## 7. Tags

Tag scenarios with Playwright's tag syntax so the tag families in
[`cucumber-tags.md`](cucumber-tags.md) (one `@functional-<area>` + at least one
lifecycle tag) carry over to selection (`--grep @smoke`):

```typescript
test('checkout completes', { tag: ['@functional-checkout', '@smoke'] }, async ({ page }) => { /* … */ });
```

## 8. Security

Generated test code meets the same security bar as production code
(`qa-standards.md` → OWASP API Top 10). The generators already enforce the rules
below — keep them when hand-editing.

- **No hard-coded secrets or URLs — env-injected only.** Base URLs and
  credentials come from the environment, never literals in the source. Generated
  specs read `process.env['API_BASE_URL']` / `process.env['UI_BASE_URL']` and
  build auth as `Authorization: Bearer ${process.env['<TOKEN_ENV>'] ?? ''}` — the
  env var *name* lives in the source, never the secret *value* (§3). A spec
  self-skips or fails loudly when a required env var is absent; it never falls
  back to an inline default credential.
- **Credentials never logged.** Never `console.log` a token, an auth header, or a
  response body that may carry a secret. Playwright prints no request headers to
  stdout, but **traces and HAR (`trace`, `--save-har`) capture the
  `Authorization` header** — treat failure artifacts as sensitive: keep them in
  the run's evidence area, never paste a raw trace into a public channel. (This
  is the Playwright equivalent of the Java `blacklistHeader("Authorization")`
  idiom — Playwright has no global request logger to filter, so the discipline is
  "do not echo or publish it".)
- **Dependencies pinned and reproducible.** Commit `package-lock.json` and install
  with `npm ci` (the generated `run-tests.sh` runs `npm ci` whenever a lockfile is
  present, falling back to `npm install` otherwise). Hash-locked installs make a
  generated suite reproducible and auditable — the npm-ecosystem equivalent of the
  OWASP dependency-check used on the Java stack.
- **No secret echoes in assertions or test data.** Assert on status and shape, not
  on secret values; keep `required_test_data` free of real credentials — use
  non-production fixtures only.

---

_Enforcement (the implementer skills emit and the static gates check these
rules) is wired in #367; the static lint ruleset is the "C2" gate. Part of
Wave 17 EPIC C (generated test-code quality), reframed from Java BDD by
ADR-0002._
