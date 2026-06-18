# Coding Standards — Playwright + TypeScript

Status: active

Ten plik = JAK piszemy generowany kod testów. Siostrzany dokument
`qa-standards.md` = JAKI jest próg jakości (ISO 25010, ISO/IEC/IEEE 29119,
ISTQB, OWASP API Top 10, WCAG 2.2). Zawsze czytaj oba. Ten dokument przepisuje
strukturę, nazwy i idiomy; `qa-standards.md` przepisuje cele pokrycia,
severity, traceability. Gdy konflikt — qa-standards wygrywa w kwestii
intencji, ten dokument w kwestii składni.

Kanoniczny stack wg
`ADR-0002`:
**`@playwright/test` (TypeScript)** — UI przez Playwright Test, API przez
`request` / `APIRequestContext`, opcjonalna DB przez typowany klient w
fixture'ach. Node.js LTS, strict TypeScript (`tsc --noEmit`), ESLint.
(Zastępuje odziedziczony `coding-standards.md` w Javie BDD.)

## 1. Warstwy i pojedyncza odpowiedzialność (SRP)

- **Jedna strona / obszar = jeden Page Object** (`LoginPage`, `CartPage`).
  **Jeden zasób API = jeden typowany klient** (`UserApiClient`).
- **Pliki spec tylko orkiestrują i asercją** — wołają Page Objecty i klientów
  API; żadnych selektorów, żadnej instalacji `fetch`/`request`, żadnej logiki
  biznesowej inline. `*.spec.ts` czyta się jak test plan.
- **Odwrócenie zależności (DIP)** — specy zależą od abstrakcji (Page Objecty,
  klienci API) wstrzykniętych jako fixture'y, nigdy od surowego `page` /
  `request` wprost.
- Jeden koncept najwyższego poziomu na plik.

## 2. Page Object Model

Page Object opakowuje jedną stronę/obszar: locatory jako pola `readonly`
budowane przez **rolę/label/tekst** (odporne, accessibility-first), oraz
metody wyrażające intencję użytkownika. Asercje trzymaj w specu (albo w
cienkich, jawnie nazwanych helperach oczekiwań) — Page Object modeluje stronę,
nie jest testem.

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
// BAD — selektory + logika biznesowa wyciekają do specu
test('login', async ({ page }) => {
  await page.goto('/login');
  await page.locator('#u').fill('a');          // surowy selektor CSS
  await page.locator('#p').fill('b');
  await page.locator('button.primary').click();
  await page.waitForTimeout(2000);             // hard wait (patrz §5)
});
```

## 3. Typowani klienci API

Opakuj `APIRequestContext` w typowanego klienta. Body request/response to
`interface`'y TypeScript — żadnego `any`. Base URL i auth pochodzą z config /
env (nigdy hardcoded, nigdy wbudowany sekret — patrz
[`docs/docker-networking-contract_pl.md`](../docker-networking-contract_pl.md)).

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

## 4. Fixture'y jako wstrzykiwanie zależności

Wstrzykuj Page Objecty, klientów API i dane testowe przez `test.extend`. Bez
mutowalnego stanu na poziomie modułu, bez singletonów, bez dzielenia między
testami — każdy test jest izolowany (Playwright daje każdemu testowi świeży
kontekst przeglądarki). Setup/teardown per-test żyje w fixture wokół `use()`.

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
// BAD — dzielony globalny stan między testami (flaky, zależny od kolejności)
let cachedUser: User;                          // mutowalny stan na poziomie modułu
test('a', async ({ userApi }) => { cachedUser = await userApi.get(1); });
test('b', async () => { expect(cachedUser.email).toBe('x'); }); // zależy od 'a'
```

## 5. Asercje i czekanie

- **Tylko web-first asercje** — `await expect(locator).toBeVisible()` /
  `toHaveText()` auto-czekają i ponawiają. Nigdy nie asercją na nieaktualnym
  snapshocie.
- **Bez hard waitów** — `page.waitForTimeout(...)` zabronione; czekaj na
  warunek (stan locatora, response, URL), nie na zegar.
- **API** — asercją status (`expect(res.ok()).toBeTruthy()` /
  `expect(res.status()).toBe(201)`) i kształt body; waliduj wobec JSON-schema
  gdy jest dostępna.

## 6. Limity rozmiaru i struktury (egzekwowane przez ruleset lint C2)

- Plik spec / Page Object / klient **≤ 300 linii**; funkcja **≤ 40 linii**;
  głębokość zagnieżdżenia **≤ 3**.
- Żadnego `any`; `strict` TypeScript; brak nieużywanych eksportów.
- Limity egzekwowane statycznie przez ruleset ESLint + `tsc --noEmit`
  generowanego frameworka (brama lint „C2"); plik ponad budżet to sygnał do
  dekompozycji — rozbij Page Object / klienta.

## 7. Tagi

Taguj scenariusze składnią tagów Playwrighta, by rodziny tagów z
[`cucumber-tags_pl.md`](cucumber-tags_pl.md) (jeden `@functional-<area>` + co
najmniej jeden tag lifecycle) przeniosły się na selekcję (`--grep @smoke`):

```typescript
test('checkout completes', { tag: ['@functional-checkout', '@smoke'] }, async ({ page }) => { /* … */ });
```

## 8. Bezpieczeństwo

Generowany kod testów spełnia ten sam próg bezpieczeństwa co kod produkcyjny
(`qa-standards_pl.md` → OWASP API Top 10). Generatory już egzekwują poniższe
reguły — utrzymaj je przy ręcznej edycji.

- **Brak zahardkodowanych sekretów i URL-i — tylko wstrzykiwanie z env.** Bazowe
  URL-e i poświadczenia pochodzą ze środowiska, nigdy z literałów w źródle.
  Generowane specy czytają `process.env['API_BASE_URL']` /
  `process.env['UI_BASE_URL']` i budują auth jako
  `Authorization: Bearer ${process.env['<TOKEN_ENV>'] ?? ''}` — w źródle jest
  *nazwa* zmiennej env, nigdy *wartość* sekretu (§3). Spec sam się pomija lub
  jawnie failuje, gdy brakuje wymaganej zmiennej env; nigdy nie ma fallbacku do
  wbudowanego poświadczenia.
- **Poświadczenia nigdy nie trafiają do logów.** Nigdy nie `console.log` tokena,
  nagłówka auth ani body odpowiedzi, które może nieść sekret. Playwright nie
  wypisuje nagłówków żądań na stdout, ale **trace i HAR (`trace`, `--save-har`)
  przechwytują nagłówek `Authorization`** — traktuj artefakty z porażek jako
  wrażliwe: trzymaj je w obszarze evidence przebiegu, nigdy nie wklejaj surowego
  trace do publicznego kanału. (To playwrightowy odpowiednik javowego idiomu
  `blacklistHeader("Authorization")` — Playwright nie ma globalnego loggera
  żądań do filtrowania, więc dyscyplina to „nie echuj i nie publikuj".)
- **Zależności przypięte i odtwarzalne.** Commituj `package-lock.json` i instaluj
  przez `npm ci` (generowany `run-tests.sh` uruchamia `npm ci`, gdy lockfile jest
  obecny, z fallbackiem do `npm install`). Instalacje z zablokowanym hashem czynią
  generowany zestaw odtwarzalnym i audytowalnym — odpowiednik OWASP
  dependency-check z ekosystemu npm, używanego na stacku Java.
- **Brak echa sekretów w asercjach i danych testowych.** Asercją status i kształt,
  nie wartości sekretów; trzymaj `required_test_data` bez prawdziwych poświadczeń
  — tylko fixture'y nieprodukcyjne.

---

_Egzekwowanie (skille implementera emitują, a bramy statyczne sprawdzają te
reguły) jest podłączone w #367; ruleset lint to brama „C2". Część Wave 17
EPIC C (jakość kodu generowanych testów), reframe z Java BDD przez ADR-0002._
