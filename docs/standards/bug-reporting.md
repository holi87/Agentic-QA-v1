# Bug Reporting Standard

Status: active

Bug reporting policy for QualityCat test framework.
Cross-references: `qa-standards.md` (ISO 25010, ISTQB), `coding-standards.md` (test code rules).

## Core Policy: Bug-Aware Testing

**Tests verify what SHOULD be per documentation, NOT what app actually returns.**

When assertion fails:
1. Check spec / requirements / OpenAPI contract first.
2. App wrong + spec right → log bug to `bugs/BUG-NNN-<slug>.md`, keep assertion as-is, mark scenario `@known-bug`.
3. Spec ambiguous → log clarification request as `Info` severity bug, decide interpretation, document in `requirements.md`.
4. Test wrong → fix test.

**Never adjust assertion to make red green.** Score depends on finding bugs, not hiding them.

## Severity Matrix

Severity = Impact × Likelihood. Use ISTQB FL classification.

| Severity | Impact | Likelihood | Examples |
|---|---|---|---|
| `Critical` | System unusable / data loss / security breach | Any | Auth bypass, SQL injection, payment lost, service crash |
| `High` | Major feature broken / wrong business value | High/Medium | Wrong total in order, broken CRUD, IDOR, missing validation on critical field |
| `Medium` | Workaround exists / partial loss of function | Any | Wrong error message, missing pagination metadata, slow response |
| `Low` | Cosmetic / edge case / minor inconsistency | Low | Typo in label, off-by-one in non-critical counter, ugly stack trace |
| `Info` | Spec ambiguity / suggestion | N/A | Missing example in docs, unclear edge case behavior |

Likelihood proxies: `High` = happens on standard happy path, `Medium` = needs specific input, `Low` = needs edge case combo.

## OWASP Mapping

Security bugs MUST cite OWASP API Top 10 2023 ID:
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

Web bugs cite OWASP Top 10 2021 (A01-A10).

## Layout: bugs/

**Layout is dictated by contest deliverables: one file per bug.** Required by jury (Testing Lab: AI Edition rules — "jeden plik = jeden bug, katalog bugs/").

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

Slug rules: lowercase kebab-case derived from title, max 50 chars, no special chars (`[^a-z0-9-]` stripped).

## Per-Bug File Schema

Each `bugs/BUG-NNN-<slug>.md` MUST contain:

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

## Mandatory Fields

Every per-bug file MUST have:
- **YAML frontmatter** with: `id`, `title`, `severity`, `likelihood`, `component`, `owasp`, `iso25010`, `wcag` (or `N/A`), `found_by`, `test`, `scenario`, `status`, `opened_at` (ISO-8601 UTC).
- **Body sections** (H2): `Steps to Reproduce`, `Expected (per spec)`, `Actual`, `Evidence`, `Impact`, `Suggested Fix`.

## bugs/README.md — Index Schema

Auto-maintained by `QC-claude-report-bug` and `scripts/new-bug.sh`. Sorted by severity desc, then ID asc.

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

## Evidence Layout

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

Trim payloads to relevant fields if > 1 MB. Anonymize PII beyond what is needed to demonstrate the bug.

## Workflow

1. Test fails in `QC-claude-verify` or scenario marked failing.
2. Triage:
   - Test broken → fix test, no bug.
   - App broken → invoke `/QC-claude-report-bug` to create new per-bug file.
3. `QC-claude-report-bug` does:
   - Auto-increment via `./scripts/new-bug.sh "<title>"` → creates `bugs/BUG-NNN-<slug>.md` skeleton.
   - Fill all mandatory fields.
   - Save evidence under `evidence/BUG-NNN/`.
   - Tag scenario with `@known-bug @bug-NNN`.
   - Allure label `severity = blocker|critical|normal|minor|trivial`.
   - Regenerate `bugs/README.md` index (sorted, totals updated).
4. Commit: `chore: report BUG-NNN <short title>`.

## Allure Severity Mapping

```java
import io.qameta.allure.Severity;
import io.qameta.allure.SeverityLevel;

@Severity(SeverityLevel.BLOCKER)   // Critical
@Severity(SeverityLevel.CRITICAL)  // High
@Severity(SeverityLevel.NORMAL)    // Medium
@Severity(SeverityLevel.MINOR)     // Low
@Severity(SeverityLevel.TRIVIAL)   // Info
```

## Anti-Patterns

- Editing assertion to make red green when app contradicts spec.
- Skipping scenario without `@known-bug` + bug ID.
- Bundling multiple defects into one bug file — one file per distinct defect.
- "It works on my machine" — always reproduce from clean state.
- Vague titles like "API broken" — name endpoint + operation + violation.
- Missing OWASP mapping on security bug.
- No suggested fix — stakeholders read missing fix as "did not investigate".
- Index `bugs/README.md` out of sync with files — regenerate after every change.

## Contest Compatibility Note

If the jury provides a different per-bug template at event start, treat it as a **field subset/superset of this schema**:
- If jury template has fewer fields → keep ours, add jury required ones in same frontmatter block.
- If jury template requires different filenames → run `scripts/migrate-bugs.sh` (or rename in place).
- Field names that conflict (e.g. `priority` vs `severity`) → keep both, mapped 1:1 in the frontmatter.

The internal QualityCat skills always read/write the schema in this file. Adapter scripts in `scripts/` translate to/from jury format.

## CLI Helper

`./scripts/new-bug.sh "<title>"` — creates `bugs/BUG-NNN-<slug>.md` skeleton, increments counter, updates `bugs/README.md` index.

## Last Updated

2026-05-11 — switched to per-file layout in `bugs/` to align with Testing Lab: AI Edition contest deliverables.
