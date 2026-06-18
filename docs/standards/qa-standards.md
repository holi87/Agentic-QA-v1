# QA and engineering standards — competition reference

Status: active

## Global agent rules

- Agent communication: always use `caveman` mode — short, technical, no fluff; disable only on explicit user request or when ambiguity would be a risk.
- Library and tool documentation: always use `Context7` before deciding on an API, runner, configuration, tags, commands, or versions; record the conclusions in decisions, reports, or review notes.
- QA + engineering standards: apply `docs/qa-standards.md` (ISO 25010 / 29119, ISTQB, TMMi, OWASP, WCAG, SOLID, 12-Factor).

> Goal: every `ailab-*` skill MUST apply the standards below. Cite the norm number in `docs/architecture.md` and `test-strategy.md` where it makes sense (Sii expects an audit trail of standards).

---

## 1. Test standards

### 1.1 ISO/IEC 25010:2023 — Software quality model
8 characteristics (each with subcharacteristics). Use as a matrix in `test-strategy.md`:

| Characteristic | Subcharacteristics | Test coverage |
|---|---|---|
| **Functional Suitability** | completeness, correctness, appropriateness | `@functional-*`, `@critical` |
| **Performance Efficiency** | time-behaviour, resource utilization, capacity | response-time assertions, load tests if time allows |
| **Compatibility** | co-existence, interoperability | API contract tests, multi-version |
| **Usability** | learnability, accessibility, UX | UI tests, WCAG (below) |
| **Reliability** | maturity, availability, fault-tolerance, recoverability | smoke, retry logic, timeouts |
| **Security** | confidentiality, integrity, non-repudiation, accountability, authenticity | `@security` (OWASP) |
| **Maintainability** | modularity, reusability, analysability, modifiability, testability | code review, SOLID |
| **Portability** | adaptability, installability, replaceability | env config, Docker |

**In the competition:** `test-strategy.md` contains a mini matrix 25010 ↔ my tests.

### 1.2 ISO/IEC/IEEE 29119 — Software Testing
5 parts:
- **29119-1** Concepts & definitions
- **29119-2** Test processes (planning, monitoring, design, execution, reporting)
- **29119-3** Documentation (Test Plan, Test Design Spec, Test Case Spec, Test Report) — templates for `docs/`
- **29119-4** Test techniques (equivalence partitioning, BVA, decision table, state transition, pairwise, exploratory)
- **29119-5** Keyword-driven testing

**In the competition:**
- `README` / `test-strategy.md` has sections aligned with 29119-3 (goals, scope, approach, risks, entry/exit criteria).
- Test cases use 29119-4 techniques (BVA → `@boundary`, equivalence → `@functional-*`, state transition → CRUD scenarios).

### 1.3 ISTQB Foundation Level (CTFL v4.0, 2023)
Key concepts to cite:

| ISTQB concept | Mapping to `ailab-*` |
|---|---|
| 7 testing principles (testing shows defects, exhaustive impossible, early testing...) | scope justification |
| Test levels: component, integration, system, acceptance | layers in `requirements.md` |
| Test types: functional, non-functional, white/black-box, change-related (regression, confirmation) | tags `@functional-*`, `@regression`, `@negative` |
| Static vs dynamic testing | code review (Codex) vs test execution |
| Test pyramid (Cohn) | more API < UI |
| Defect lifecycle (new → assigned → fixed → verified → closed) | `bugs/*.md` template |
| Risk-based testing | `requirements.md` section "Risks" |

**In the competition:** `test-strategy.md` cites the ISTQB pyramid + 7 principles + risk-based approach.

### 1.4 ISTQB Advanced (selectively, if time permits)
- **CTAL-TA** (Test Analyst) — black-box techniques in detail
- **CTAL-TTA** (Technical Test Analyst) — white-box + non-functional
- **CTAL-TM** (Test Manager) — planning, risk, metrics

**In the competition:** rare, but worth keeping the "metrics" category in mind (defect density, test coverage %, MTBF).

### 1.5 TMMi (Test Maturity Model integration)
5 maturity levels:
1. Initial (chaos)
2. Managed (test policy, planning, monitoring, design, execution, environment)
3. Defined (test organization, training, lifecycle, peer reviews, non-functional)
4. Measured (test measurement, product quality evaluation, advanced reviews)
5. Optimization (defect prevention, QC, test process optimization)

**In the competition:** `docs/architecture.md` may mention that the framework targets TMMi level 3 (lifecycle, peer reviews via Codex, non-functional via `@security`/`@performance`).

### 1.6 ISO 9126 (predecessor of 25010, deprecated 2011)
Do not use — reference 25010 instead.

---

## 2. Security standards

### 2.1 OWASP Top 10 (2021, 2025 edition still in development)
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

**In the competition:** every `tests/api/test_security.py` (or `security.spec.ts`) template MUST cover at least 5 of the above.

### 2.3 OWASP ASVS (Application Security Verification Standard) v4.0.3
A list of requirements to verify. Three levels: L1 (cursory), L2 (most apps), L3 (high security). Cite the categories (V1 architecture, V2 auth, V3 session, V4 access control, V5 validation, V7 errors&logging, V9 communications, V13 API).

### 2.4 ISO/IEC 27001:2022
Information Security Management System. In the context of the competition tests: mention in `docs/architecture.md` that the framework supports controls (A.5.23 cloud security, A.8.28 secure coding).

### 2.5 CWE Top 25 (Most Dangerous Software Weaknesses)
Cite CWE numbers in bug reports: `CWE-79 (XSS)`, `CWE-89 (SQLi)`, `CWE-22 (Path Traversal)`.

---

## 3. Accessibility and UX standards

### 3.1 WCAG 2.2 (W3C Recommendation, 2023)
4 principles (POUR): Perceivable, Operable, Understandable, Robust.
3 levels: A, AA, AAA. Target: AA.

**In the competition (UI tests):**
- alt text on images (1.1.1)
- contrast 4.5:1 (1.4.3)
- keyboard navigation (2.1.1)
- focus visible (2.4.7)
- form labels (3.3.2)

**Tooling:** `@axe-core/playwright`, `pa11y`. Tag: `@accessibility`.

**A11y acceptance indicators:** every UI candidate review must mark whether
the flow needs accessibility coverage. If it does, at least one generated or
manual test case must name the concrete indicator it verifies:

- semantic name/role exposed for the primary control or region;
- full keyboard path through the flow, including no keyboard trap;
- visible focus state on every interactive element reached by the test;
- form fields have programmatic labels and error text is announced;
- text and essential icons meet WCAG AA contrast;
- non-text content has useful alternative text or is hidden from assistive tech;
- dynamic status/error updates use an appropriate live region.

Do not treat `@accessibility` as a late add-on. Link these indicators from
`requirements.md` risk notes and `TEST-PLAN.md` candidate rows before
`implement-tests` runs.

### 3.2 ISO 9241-110:2020 — Dialogue principles
Suitability for task, self-descriptiveness, conformity with expectations, learnability, controllability, error tolerance, individualisation. Cite in the UX rationale.

---

## 4. Engineering standards (cross-language)

### 4.1 SOLID (Robert C. Martin)
- **S**ingle Responsibility — class does 1 thing
- **O**pen/Closed — open for extension, closed for modification
- **L**iskov Substitution — subtype == supertype in use
- **I**nterface Segregation — narrow interfaces
- **D**ependency Inversion — depend on abstractions

**In the competition:** Page Objects, ApiClient, fixtures — each must have SRP.

### 4.2 Clean Code (Robert C. Martin)
- Names: meaningful, intention-revealing, no encodings (`m_var`, `strName`)
- Functions: small, single level of abstraction, ≤3 arguments
- Comments: only why, not what
- Test FIRST (Fast, Independent, Repeatable, Self-validating, Timely)

### 4.3 DRY / KISS / YAGNI / SLAP
- DRY — Don't Repeat Yourself
- KISS — Keep It Simple, Stupid
- YAGNI — You Aren't Gonna Need It
- SLAP — Single Level of Abstraction Principle

### 4.4 12-Factor App (Heroku, 2017)
- Codebase, dependencies, config (env vars), backing services, build/release/run, processes (stateless), port binding, concurrency, disposability, dev/prod parity, logs (stdout), admin processes.

**In the competition:** config via env vars + `.env.example` in the repo, no hardcoded URLs.

### 4.5 Conventional Commits 1.0.0
Format: `type(scope): subject`
- type: feat, fix, docs, style, refactor, test, chore, ci, build, perf
- breaking change: `!` after type or `BREAKING CHANGE:` in footer

**In the competition:** every commit conforms.

### 4.6 Semantic Versioning 2.0.0
MAJOR.MINOR.PATCH — breaking / feature / bugfix.

---

## 5. Per-language / stack standards

### 5.1 Python
- **PEP 8** — style (4 spaces, line ≤79/99, naming snake_case)
- **PEP 257** — docstrings
- **PEP 484** — type hints (use everywhere)
- **PEP 561** — package distributing types
- **PEP 621** — `pyproject.toml` (preferred over setup.py)
- Tooling: `black`, `ruff`, `mypy --strict`, `pytest --strict-markers`
- Pytest: AAA pattern, fixtures in `conftest.py`, parametrize instead of loops

### 5.2 TypeScript
- **TC39** — proposals (Stage 4 only in production)
- `tsconfig`: `strict: true`, `noImplicitAny`, `strictNullChecks`
- Style: Airbnb TS or Standard
- Tooling: `eslint`, `prettier`, `tsc --noEmit`
- Playwright: Page Object Model, fixtures, `test.describe` blocks, expect with auto-retry

### 5.3 Java
- **JLS** (Java Language Specification, Java 21 LTS / Java 17)
- Google Java Style Guide
- JUnit 5 (`@Test`, `@Tag`, `@DisplayName`, `@ParameterizedTest`)
- AssertJ over Hamcrest for readable assertions
- Maven Standard Directory Layout (`src/main/java`, `src/test/java`)
- Effective Java (Joshua Bloch) — 90 items; key ones: prefer composition, builders, immutability, fail-fast

---

## 6. Documentation standards

### 6.1 Markdown
- **CommonMark 0.31** — spec
- GitHub Flavored Markdown (GFM) for tables + checkboxes

### 6.2 IEEE 829 (superseded by 29119-3, but legacy)
Test Plan structure — compatible with 29119-3.

### 6.3 Arc42 / C4 Model
- **arc42** — architecture document template (12 sections)
- **C4** (Context, Container, Component, Code) — diagrams

**In the competition:** `docs/architecture.md` uses C4 or arc42 sections 1-6 (intro, constraints, context, solution strategy, building blocks, runtime).

---

## 7. Mapping standards to competition artifacts

| Artifact | Required standards |
|---|---|
| `requirements.md` | ISO 29119-2 (planning), ISTQB risk-based, OWASP risks, A11y indicators for UI risks |
| `DECISION.md` | 12-Factor (config), SemVer (lib versions) |
| `tests/**` | ISO 29119-4 (techniques), ISTQB tags, OWASP API Top 10, WCAG (UI), A11y indicator assertions, PEP 8 / Airbnb TS / Google Java |
| `docs/architecture.md` | arc42 / C4, ISO 25010, TMMi level reference |
| `docs/test-strategy.md` | ISO 29119-3, ISTQB pyramid + 7 principles, ISO 25010 matrix |
| `README.md` | CommonMark, 12-Factor (env setup), Conventional Commits |
| `bugs/*.md` | ISTQB defect lifecycle, CWE numbers, OWASP category |
| Commits | Conventional Commits 1.0.0 |
| `.gitignore` | OWASP A05 (no secrets), 12-Factor (dev/prod parity) |

---

## 8. Quick reference — what to cite in which file

```
test-strategy.md section "Approach":
  We use ISO 29119-4 techniques: BVA (@boundary), equivalence partitioning
  (@functional-*), decision table (CRUD scenarios), exploratory (Round 2).
  ISO/IEC 25010 characteristics covered: Functional Suitability, Reliability,
  Security (OWASP API Top 10), Maintainability.

architecture.md section "Quality":
  Framework targets TMMi level 3: lifecycle, peer reviews (Codex), non-functional
  (@security, @performance). Architecture aligned with C4 model (below: Container
  + Component diagrams).

bugs/BUG-001.md:
  Severity: Critical
  CWE: CWE-89 (SQL Injection)
  OWASP: API3:2023 Broken Object Property Level Authorization
  ISTQB defect status: New
```

---

## 9. Anti-patterns (what NOT to do)

- Tests dependent on execution order (violates ISTQB principle "tests independent")
- Assertions without an error message (violates Clean Code)
- Hardcoded URLs/credentials (violates 12-Factor + OWASP A05)
- Silent catch blocks (violates 29119-4 + OWASP A09 logging)
- Mocks in integration tests (violates ISTQB test level definition)
- `time.sleep()` in async tests (flaky, violates F.I.R.S.T.)
- "What" comments instead of "why" (violates Clean Code)
- A test file without a tag (violates the CLAUDE.md tagging policy)

---

In total: ~25 standards actively used in the competition. Cite norm numbers in the docs — Sii values the audit trail of standards (TMMi level 3+).
