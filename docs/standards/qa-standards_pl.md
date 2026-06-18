# Standardy QA i programistyczne — referencja konkursowa

Status: active

## Zasady globalne agentów

- Komunikacja agentów: zawsze tryb `caveman` — krótko, technicznie, bez fluffu; wyłącz tylko na wyraźne polecenie użytkownika albo przy ryzyku niejednoznaczności.
- Dokumentacja bibliotek i narzędzi: zawsze użyj `Context7` przed decyzją o API, runnerze, konfiguracji, tagach, komendach lub wersjach; wnioski zapisuj w decyzjach, raportach albo review notes.
- Standardy QA + programistyczne: stosuj `docs/qa-standards.md` (ISO 25010 / 29119, ISTQB, TMMi, OWASP, WCAG, SOLID, 12-Factor).

> Cel: każdy skill `ailab-*` MUSI stosować poniższe standardy. Cytuj numer normy w `docs/architecture.md` i `test-strategy.md`, gdzie to ma sens (Sii oczekuje śladów standardów).

---

## 1. Standardy testowe

### 1.1 ISO/IEC 25010:2023 — Model jakości oprogramowania
8 charakterystyk (każda z subcharakterystykami). Stosuj jako matrycę w `test-strategy.md`:

| Charakterystyka | Subcharakterystyki | Pokrycie testowe |
|---|---|---|
| **Functional Suitability** | completeness, correctness, appropriateness | `@functional-*`, `@critical` |
| **Performance Efficiency** | time-behaviour, resource utilization, capacity | response-time assertions, load tests jeśli czas |
| **Compatibility** | co-existence, interoperability | API contract tests, multi-version |
| **Usability** | learnability, accessibility, UX | UI tests, WCAG (poniżej) |
| **Reliability** | maturity, availability, fault-tolerance, recoverability | smoke, retry logic, timeouts |
| **Security** | confidentiality, integrity, non-repudiation, accountability, authenticity | `@security` (OWASP) |
| **Maintainability** | modularity, reusability, analysability, modifiability, testability | code review, SOLID |
| **Portability** | adaptability, installability, replaceability | env config, Docker |

**W konkursie:** `test-strategy.md` zawiera mini-matrycę 25010 ↔ moje testy.

### 1.2 ISO/IEC/IEEE 29119 — Software Testing
5 części:
- **29119-1** Concepts & definitions
- **29119-2** Test processes (planning, monitoring, design, execution, reporting)
- **29119-3** Documentation (Test Plan, Test Design Spec, Test Case Spec, Test Report) — szablony do `docs/`
- **29119-4** Test techniques (equivalence partitioning, BVA, decision table, state transition, pairwise, exploratory)
- **29119-5** Keyword-driven testing

**W konkursie:**
- `README` / `test-strategy.md` ma sekcje zgodne z 29119-3 (cele, scope, podejście, ryzyka, kryteria entry/exit).
- Test cases używają technik 29119-4 (BVA → `@boundary`, equivalence → `@functional-*`, state transition → CRUD scenariusze).

### 1.3 ISTQB Foundation Level (CTFL v4.0, 2023)
Kluczowe pojęcia do cytowania:

| Koncept ISTQB | Mapowanie `ailab-*` |
|---|---|
| 7 zasad testowania (testing shows defects, exhaustive impossible, early testing...) | uzasadnienie scope |
| Test levels: component, integration, system, acceptance | warstwy w `requirements.md` |
| Test types: functional, non-functional, white/black-box, change-related (regression, confirmation) | tagi `@functional-*`, `@regression`, `@negative` |
| Static vs dynamic testing | code review (Codex) vs test execution |
| Test pyramid (Cohn) | więcej API < UI |
| Defect lifecycle (new → assigned → fixed → verified → closed) | szablon `bugs/*.md` |
| Risk-based testing | sekcja "Ryzyka" w `requirements.md` |

**W konkursie:** `test-strategy.md` cytuje ISTQB pyramid + 7 principles + risk-based approach.

### 1.4 ISTQB Advanced (selektywnie, jeśli czas)
- **CTAL-TA** (Test Analyst) — techniki czarnoskrzynkowe szczegółowo
- **CTAL-TTA** (Technical Test Analyst) — biało-skrzynkowe + non-functional
- **CTAL-TM** (Test Manager) — planowanie, ryzyko, metryki

**W konkursie:** rzadko, ale warto mieć w głowie kategorię "metryki" (defect density, test coverage %, MTBF).

### 1.5 TMMi (Test Maturity Model integration)
5 poziomów dojrzałości:
1. Initial (chaos)
2. Managed (test policy, planning, monitoring, design, execution, environment)
3. Defined (test organization, training, lifecycle, peer reviews, non-functional)
4. Measured (test measurement, product quality evaluation, advanced reviews)
5. Optimization (defect prevention, QC, test process optimization)

**W konkursie:** `docs/architecture.md` może wzmiankować, że framework celuje w TMMi level 3 (lifecycle, peer reviews przez Codex, non-functional przez `@security`/`@performance`).

### 1.6 ISO 9126 (poprzednik 25010, deprecated 2011)
Nie używaj — referuj 25010.

---

## 2. Standardy bezpieczeństwa

### 2.1 OWASP Top 10 (2021, edycja 2025 dopiero w opracowaniu)
1. Broken Access Control → IDOR test (`@security`)
2. Cryptographic Failures → HTTPS, TLS, hash test
3. Injection → SQLi/NoSQLi/CMD injection
4. Insecure Design → architectural review
5. Security Misconfiguration → headers (CSP, HSTS, X-Frame-Options)
6. Vulnerable Components → `npm audit`, `pip-audit`, `mvn dependency-check`
7. Identification & Auth Failures → brute-force, session fixation
8. Software & Data Integrity Failures → CSRF, supply chain
9. Logging & Monitoring Failures → log presence test
10. SSRF → URL validation

### 2.2 OWASP API Security Top 10 (2023)
1. Broken Object Level Authorization (BOLA = IDOR)
2. Broken Authentication
3. Broken Object Property Level Authorization (mass assignment, excessive data exposure)
4. Unrestricted Resource Consumption
5. Broken Function Level Authorization
6. Unrestricted Access to Sensitive Business Flows
7. Server Side Request Forgery (SSRF)
8. Security Misconfiguration
9. Improper Inventory Management (versioning, deprecated endpoints)
10. Unsafe Consumption of APIs

**W konkursie:** każdy szablon `tests/api/test_security.py` (lub `security.spec.ts`) MUSI mieć min. 5 z powyższych.

### 2.3 OWASP ASVS (Application Security Verification Standard) v4.0.3
Lista wymagań do weryfikacji. Trzy poziomy: L1 (cursory), L2 (most apps), L3 (high security). Cytuj kategorie (V1 architecture, V2 auth, V3 session, V4 access control, V5 validation, V7 errors&logging, V9 communications, V13 API).

### 2.4 ISO/IEC 27001:2022
Information Security Management System. W kontekście testów konkursu: wzmianka w `docs/architecture.md`, że framework wspiera kontrole (A.5.23 cloud security, A.8.28 secure coding).

### 2.5 CWE Top 25 (Most Dangerous Software Weaknesses)
Cytuj numery CWE w bug reportach: `CWE-79 (XSS)`, `CWE-89 (SQLi)`, `CWE-22 (Path Traversal)`.

---

## 3. Standardy dostępności i UX

### 3.1 WCAG 2.2 (W3C Recommendation, 2023)
4 zasady (POUR): Perceivable, Operable, Understandable, Robust.
3 poziomy: A, AA, AAA. Cel: AA.

**W konkursie (UI tests):**
- alt text na obrazach (1.1.1)
- kontrast 4.5:1 (1.4.3)
- keyboard navigation (2.1.1)
- focus visible (2.4.7)
- form labels (3.3.2)

**Tooling:** `@axe-core/playwright`, `pa11y`. Tag: `@accessibility`.

**Wyznaczniki akceptacji A11y:** każdy review kandydatów UI musi oznaczyć,
czy flow wymaga pokrycia dostępności. Jeśli tak, co najmniej jeden
wygenerowany albo manualny przypadek musi nazwać konkretny wyznacznik:

- semantyczna nazwa/rola dla głównej kontrolki albo regionu;
- pełna ścieżka klawiaturą przez flow, bez pułapki klawiaturowej;
- widoczny focus na każdym elemencie interaktywnym osiąganym przez test;
- pola formularzy mają programatyczne etykiety, a błędy są ogłaszane;
- tekst i istotne ikony spełniają kontrast WCAG AA;
- treści nietekstowe mają użyteczny alt albo są ukryte przed assistive tech;
- dynamiczne statusy/błędy używają właściwego live region.

Nie traktuj `@accessibility` jako dodatku na końcu. Linkuj te wyznaczniki z
ryzyk w `requirements.md` i wierszy kandydatów w `TEST-PLAN.md` przed
uruchomieniem `implement-tests`.

### 3.2 ISO 9241-110:2020 — Dialogue principles
Suitability for task, self-descriptiveness, conformity with expectations, learnability, controllability, error tolerance, individualisation. Cytuj w UX rationale.

---

## 4. Standardy programistyczne (cross-language)

### 4.1 SOLID (Robert C. Martin)
- **S**ingle Responsibility — klasa robi 1 rzecz
- **O**pen/Closed — open for extension, closed for modification
- **L**iskov Substitution — podtyp == nadtyp w użyciu
- **I**nterface Segregation — wąskie interfejsy
- **D**ependency Inversion — depend on abstractions

**W konkursie:** Page Objects, ApiClient, fixtures — każdy musi mieć SRP.

### 4.2 Clean Code (Robert C. Martin)
- Nazwy: meaningful, intention-revealing, no encodings (`m_var`, `strName`)
- Funkcje: małe, jeden poziom abstrakcji, ≤3 argumenty
- Komentarze: tylko gdy "dlaczego" (why), nie "co" (what)
- Test FIRST (Fast, Independent, Repeatable, Self-validating, Timely)

### 4.3 DRY / KISS / YAGNI / SLAP
- DRY — Don't Repeat Yourself
- KISS — Keep It Simple, Stupid
- YAGNI — You Aren't Gonna Need It
- SLAP — Single Level of Abstraction Principle

### 4.4 12-Factor App (Heroku, 2017)
- Codebase, dependencies, config (env vars), backing services, build/release/run, processes (stateless), port binding, concurrency, disposability, dev/prod parity, logs (stdout), admin processes.

**W konkursie:** config przez env vars + `.env.example` w repo, brak hardkodowanych URLi.

### 4.5 Conventional Commits 1.0.0
Format: `type(scope): subject`
- type: feat, fix, docs, style, refactor, test, chore, ci, build, perf
- breaking change: `!` po type lub `BREAKING CHANGE:` w footer

**W konkursie:** każdy commit zgodny.

### 4.6 Semantic Versioning 2.0.0
MAJOR.MINOR.PATCH — breaking / feature / bugfix.

---

## 5. Standardy per język/stack

### 5.1 Python
- **PEP 8** — style (4 spaces, line ≤79/99, naming snake_case)
- **PEP 257** — docstrings
- **PEP 484** — type hints (stosuj wszędzie)
- **PEP 561** — package distributing types
- **PEP 621** — `pyproject.toml` (preferowane nad setup.py)
- Tooling: `black`, `ruff`, `mypy --strict`, `pytest --strict-markers`
- Pytest: AAA pattern, fixtures w `conftest.py`, parametrize zamiast pętli

### 5.2 TypeScript
- **TC39** — proposals (do produkcji tylko Stage 4)
- `tsconfig`: `strict: true`, `noImplicitAny`, `strictNullChecks`
- Style: Airbnb TS lub Standard
- Tooling: `eslint`, `prettier`, `tsc --noEmit`
- Playwright: Page Object Model, fixtures, bloki `test.describe`, expect with auto-retry

### 5.3 Java
- **JLS** (Java Language Specification, Java 21 LTS / Java 17)
- Google Java Style Guide
- JUnit 5 (`@Test`, `@Tag`, `@DisplayName`, `@ParameterizedTest`)
- AssertJ zamiast Hamcrest dla czytelnych asercji
- Maven Standard Directory Layout (`src/main/java`, `src/test/java`)
- Effective Java (Joshua Bloch) — 90 itemów; kluczowe: prefer composition, builders, immutability, fail-fast

---

## 6. Standardy dokumentacji

### 6.1 Markdown
- **CommonMark 0.31** — spec
- GitHub Flavored Markdown (GFM) dla tabel + checkbox

### 6.2 IEEE 829 (zastąpiony przez 29119-3, ale legacy)
Test Plan structure — kompatybilna z 29119-3.

### 6.3 Arc42 / C4 Model
- **arc42** — szablon architecture document (12 sekcji)
- **C4** (Context, Container, Component, Code) — diagramy

**W konkursie:** `docs/architecture.md` używa C4 albo arc42 sekcji 1-6 (intro, constraints, context, solution strategy, building blocks, runtime).

---

## 7. Mapowanie standardów na artefakty konkursowe

| Artefakt | Wymagane standardy |
|---|---|
| `requirements.md` | ISO 29119-2 (planning), ISTQB risk-based, OWASP ryzyka, wyznaczniki A11y dla ryzyk UI |
| `DECISION.md` | 12-Factor (config), SemVer (wersje libów) |
| `tests/**` | ISO 29119-4 (techniques), ISTQB tags, OWASP API Top 10, WCAG (UI), asercje wyznaczników A11y, PEP 8 / Airbnb TS / Google Java |
| `docs/architecture.md` | arc42 / C4, ISO 25010, TMMi level reference |
| `docs/test-strategy.md` | ISO 29119-3, ISTQB pyramid + 7 principles, ISO 25010 matrix |
| `README.md` | CommonMark, 12-Factor (env setup), Conventional Commits |
| `bugs/*.md` | ISTQB defect lifecycle, CWE numbers, OWASP category |
| Commits | Conventional Commits 1.0.0 |
| `.gitignore` | OWASP A05 (no secrets), 12-Factor (dev/prod parity) |

---

## 8. Quick reference — co cytować w jakim pliku

```
test-strategy.md sekcja "Approach":
  Stosujemy ISO 29119-4 techniques: BVA (@boundary), equivalence partitioning
  (@functional-*), decision table (CRUD scenarios), exploratory (Runda 2).
  Pokrycie ISO/IEC 25010 charakterystyk: Functional Suitability, Reliability,
  Security (OWASP API Top 10), Maintainability.

architecture.md sekcja "Quality":
  Framework wspiera TMMi level 3: lifecycle, peer reviews (Codex), non-functional
  (@security, @performance). Architektura zgodna z C4 model (poniżej Container
  + Component diagrams).

bugs/BUG-001.md:
  Severity: Critical
  CWE: CWE-89 (SQL Injection)
  OWASP: API3:2023 Broken Object Property Level Authorization
  ISTQB defect status: New
```

---

## 9. Anti-patterns (czego NIE robić)

- Testy zależne od kolejności wykonania (narusza ISTQB principle "tests independent")
- Asercje bez wiadomości błędu (narusza Clean Code)
- Hardcoded URLs/credentials (narusza 12-Factor + OWASP A05)
- Silent catch blocks (narusza 29119-4 + OWASP A09 logging)
- Mock w testach integracyjnych (narusza ISTQB test level definition)
- `time.sleep()` w testach async (flaky, narusza F.I.R.S.T.)
- Komentarze "co" zamiast "dlaczego" (narusza Clean Code)
- Plik testowy bez tagu (narusza CLAUDE.md tagging policy)

---

Łącznie: ~25 standardów aktywnie używanych w konkursie. Cytuj numerami norm w docs — Sii ceni ślad standardów (TMMi level 3+).
