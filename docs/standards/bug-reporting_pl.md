# Standard zgłaszania bugów

Status: active

Polityka zgłaszania bugów dla frameworka testowego QualityCat.
Powiązania: `qa-standards.md` (ISO 25010, ISTQB), `coding-standards.md` (reguły kodu testowego).

## Główna polityka: Bug-Aware Testing

**Testy weryfikują to, co POWINNO być wg dokumentacji, NIE to, co aplikacja faktycznie zwraca.**

Gdy asercja oblewa:
1. Najpierw sprawdź spec / wymagania / kontrakt OpenAPI.
2. Aplikacja zła + spec poprawny → zaloguj bug do `bugs/BUG-NNN-<slug>.md`, zachowaj asercję bez zmian, oznacz scenariusz `@known-bug`.
3. Spec niejednoznaczny → zaloguj prośbę o wyjaśnienie jako bug severity `Info`, zdecyduj interpretację, udokumentuj w `requirements.md`.
4. Test zły → napraw test.

**Nigdy nie dostosowuj asercji, by zazielenić czerwone.** Punktacja zależy od znajdowania bugów, nie ukrywania ich.

## Macierz severity

Severity = Impact × Likelihood. Użyj klasyfikacji ISTQB FL.

| Severity | Impact | Likelihood | Przykłady |
|---|---|---|---|
| `Critical` | System nieużywalny / utrata danych / naruszenie bezpieczeństwa | Any | Auth bypass, SQL injection, payment lost, service crash |
| `High` | Złamana główna funkcja / błędna wartość biznesowa | High/Medium | Błędna suma w zamówieniu, broken CRUD, IDOR, brak walidacji na krytycznym polu |
| `Medium` | Istnieje obejście / częściowa utrata funkcji | Any | Błędny komunikat błędu, brak metadanych paginacji, wolna odpowiedź |
| `Low` | Kosmetyka / edge case / drobna niespójność | Low | Literówka w etykiecie, off-by-one w niekrytycznym liczniku, brzydki stack trace |
| `Info` | Niejednoznaczność specyfikacji / sugestia | N/A | Brak przykładu w dokumentacji, niejasne zachowanie edge case |

Proxy likelihood: `High` = dzieje się na standardowym happy path, `Medium` = wymaga konkretnego inputu, `Low` = wymaga kombinacji edge case.

## Mapowanie OWASP

Bugi security MUSZĄ cytować ID OWASP API Top 10 2023:
- API1 Broken Object Level Authorization (IDOR)
- API2 Broken Authentication
- API3 Broken Object Property Level Authorization (mass assignment, excessive data exposure)
- API4 Unrestricted Resource Consumption
- API5 Broken Function Level Authorization
- API6 Unrestricted Access to Sensitive Business Flows
- API7 Server Side Request Forgery
- API8 Security Misconfiguration
- API9 Improper Inventory Management
- API10 Unsafe Consumption of APIs

Bugi web cytują OWASP Top 10 2021 (A01-A10).

## Layout: bugs/

**Layout podyktowany przez deliverables konkursu: jeden plik na bug.** Wymagane przez jury (Testing Lab: AI Edition rules — "jeden plik = jeden bug, katalog bugs/").

```
<project-root>/
├── bugs/
│   ├── README.md                       # summary table — index of all bugs (sorted by severity desc)
│   ├── BUG-001-idor-users-endpoint.md
│   ├── BUG-002-total-mismatch-orders.md
│   └── ...
└── evidence/
    ├── BUG-001/
    │   ├── response.json
    │   └── repro.curl
    └── BUG-002/
        └── screenshot.png
```

Reguły slug: lowercase kebab-case wyprowadzony z tytułu, maks. 50 znaków, bez znaków specjalnych (`[^a-z0-9-]` usuwane).

## Schemat pliku per bug

Każdy `bugs/BUG-NNN-<slug>.md` MUSI zawierać:

```markdown
---
id: BUG-001
title: IDOR on /api/users/{id}
severity: Critical
likelihood: High
component: API / Authorization
owasp: API1 Broken Object Level Authorization
iso25010: Security / Confidentiality
wcag: N/A
found_by: QC-claude-implement-api / Cucumber tag @security
test: src/test/java/pl/qualitycat/api/SecurityIdorTest.java::userCanAccessOtherUserData
scenario: src/test/resources/features/api/users-security.feature::Cross-tenant read
status: OPEN
opened_at: 2026-05-11T10:30:00Z
---

# BUG-001: IDOR on /api/users/{id}

## Steps to Reproduce

1. Login as user A (id=1) via `POST /api/auth/login`; capture access token.
2. With token A, call `GET /api/users/2`.
3. Observe: server returns user 2 profile body.

Repro command:
```bash
curl -H "Authorization: Bearer <tokenA>" http://localhost:3001/api/users/2
```

## Expected (per spec)

OpenAPI section `/api/users/{id}` states: "Returns 403 Forbidden if the authenticated user does not own the resource and is not an admin."

```
HTTP/1.1 403 Forbidden
{ "error": "access_denied" }
```

## Actual

```
HTTP/1.1 200 OK
{ "id": 2, "email": "userB@example.com", "role": "user", ... }
```

## Evidence

- `evidence/BUG-001/response.json` — raw 200 response body.
- `evidence/BUG-001/repro.curl` — copy-pasteable repro command.
- `evidence/BUG-001/openapi-excerpt.yaml` — spec section quoted.

## Impact

Cross-tenant data leak. Any authenticated user reads any other user's PII (email, role, audit metadata). Single OWASP API1 violation, CVSS-likely High.

## Suggested Fix

Enforce object-level authorization in the controller for `/api/users/{id}`:

```java
if (!request.userId.equals(pathUserId) && !request.hasRole("admin")) {
    throw new ForbiddenException();
}
```

Add integration test covering both same-user and cross-user paths.
```

## Pola obowiązkowe

Każdy plik per bug MUSI mieć:
- **YAML frontmatter** z: `id`, `title`, `severity`, `likelihood`, `component`, `owasp`, `iso25010`, `wcag` (lub `N/A`), `found_by`, `test`, `scenario`, `status`, `opened_at` (ISO-8601 UTC).
- **Sekcje body** (H2): `Steps to Reproduce`, `Expected (per spec)`, `Actual`, `Evidence`, `Impact`, `Suggested Fix`.

## bugs/README.md — schemat indeksu

Auto-utrzymywany przez `QC-claude-report-bug` i `scripts/new-bug.sh`. Sortowany malejąco po severity, potem rosnąco po ID.

```markdown
# Bug Index

Total: 8 (3 Critical, 2 High, 2 Medium, 1 Low, 0 Info)

| ID | Severity | Title | Component | OWASP | Status |
|---|---|---|---|---|---|
| BUG-001 | Critical | IDOR on /api/users/{id} | API / Authorization | API1 | OPEN |
| BUG-002 | High | Total mismatch in /api/orders | API / Logic | N/A | OPEN |
| ...

See `qualitycat-standards/bug-reporting.md` for full schema.
```

## Layout evidence

```
evidence/BUG-NNN/
├── response.json        # API: raw response body
├── request.http         # API: full HTTP request (headers, body)
├── repro.curl           # Copy-paste repro command
├── screenshot.png       # UI: Playwright screenshot
├── trace.zip            # UI: Playwright trace (if captured)
├── console.log          # UI: browser console output
└── openapi-excerpt.yaml # Spec excerpt cited in Expected section
```

Przytnij payloady do istotnych pól jeśli > 1 MB. Anonimizuj PII poza tym, co potrzebne, by zademonstrować buga.

## Workflow

1. Test oblewa w `QC-claude-verify` lub scenariusz oznaczony jako oblany.
2. Triage:
   - Test zepsuty → popraw test, brak buga.
   - Aplikacja zepsuta → wywołaj `/QC-claude-report-bug`, by utworzyć nowy plik per bug.
3. `QC-claude-report-bug` robi:
   - Auto-inkrementacja przez `./scripts/new-bug.sh "<title>"` → tworzy szkielet `bugs/BUG-NNN-<slug>.md`.
   - Wypełnia wszystkie pola obowiązkowe.
   - Zapisuje evidence pod `evidence/BUG-NNN/`.
   - Taguje scenariusz `@known-bug @bug-NNN`.
   - Etykieta Allure `severity = blocker|critical|normal|minor|trivial`.
   - Regeneruje indeks `bugs/README.md` (posortowany, sumy zaktualizowane).
4. Commit: `chore: report BUG-NNN <short title>`.

## Mapowanie severity Allure

```java
import io.qameta.allure.Severity;
import io.qameta.allure.SeverityLevel;

@Severity(SeverityLevel.BLOCKER)   // Critical
@Severity(SeverityLevel.CRITICAL)  // High
@Severity(SeverityLevel.NORMAL)    // Medium
@Severity(SeverityLevel.MINOR)     // Low
@Severity(SeverityLevel.TRIVIAL)   // Info
```

## Anty-wzorce

- Edycja asercji, by zazielenić czerwone, gdy aplikacja jest sprzeczna ze spec.
- Pomijanie scenariusza bez `@known-bug` + bug ID.
- Pakowanie wielu defektów w jeden plik buga — jeden plik na odrębny defekt.
- "U mnie działa" — zawsze reprodukuj od czystego stanu.
- Mgliste tytuły jak "API broken" — nazwij endpoint + operację + naruszenie.
- Brak mapowania OWASP na bugu security.
- Brak suggested fix — interesariusze czytają brak fixa jako "nie zbadano".
- Indeks `bugs/README.md` rozjechany z plikami — regeneruj po każdej zmianie.

## Nota o kompatybilności konkursu

Jeśli jury dostarczy inny szablon per bug na starcie wydarzenia, traktuj go jako **subset/superset pól tego schematu**:
- Jeśli szablon jury ma mniej pól → zachowaj nasze, dodaj wymagane przez jury w tym samym bloku frontmatter.
- Jeśli szablon jury wymaga innych nazw plików → uruchom `scripts/migrate-bugs.sh` (lub zmień nazwy w miejscu).
- Konfliktujące nazwy pól (np. `priority` vs `severity`) → zachowaj oba, zmapowane 1:1 w frontmatter.

Wewnętrzne skille QualityCat zawsze czytają/zapisują schemat z tego pliku. Skrypty adaptera w `scripts/` tłumaczą do/z formatu jury.

## Helper CLI

`./scripts/new-bug.sh "<title>"` — tworzy szkielet `bugs/BUG-NNN-<slug>.md`, inkrementuje licznik, aktualizuje indeks `bugs/README.md`.

## Ostatnia aktualizacja

2026-05-11 — przełączono na layout per-file w `bugs/`, by dopasować się do deliverables konkursu Testing Lab: AI Edition.
