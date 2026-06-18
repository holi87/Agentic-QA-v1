---
name: qc-codex-reviewer-validate-security
description: "Security Engineer reviewer. Two concerns: (A) does the framework TEST security properly (OWASP API Top 10 / OWASP Top 10 / WCAG coverage)? (B) does the framework ITSELF have security issues (hardcoded creds, leaked tokens, insecure deps)?"
---

# Skill: qc-codex-reviewer-validate-security

## Communication

${include_preamble}

## Standards

This skill audits against shared conventions — read the canonical source, do not restate it:

- Generated-code structure & idioms (Playwright + TypeScript) — `docs/standards/playwright-ts-standards.md` (§3 typed clients & env-injected base URL/auth, §7 tags, §8 security).
- Quality bar (OWASP API Top 10, OWASP Top 10, WCAG 2.2) — `docs/standards/qa-standards.md`.

## When to use

- After Round 2 implementation (`@security @boundary @extended` slice).
- BEFORE final-gate.
- NOT during implementation; NOT during initial design.

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- `docs/severity-policy.md` or the OWASP mapping reference is missing → `needs_input: severity_policy`.
- no tests exist under `tests/` to audit security coverage against → `needs_input: tests`.
- `package.json` (or the equivalent dependency manifest) is absent → `needs_input: dependency_manifest`.

## What to do

Part A — Test Coverage of Security:
1. OWASP API Top 10 (2023) coverage per item: API1 BOLA/IDOR (switch user IDs, expect 403/404); API2 Broken Auth (missing/expired/wrong-issuer token); API3 BOPLA (mass assignment via `role: admin` in update); API4 Unrestricted Resource Consumption (large payloads, rapid calls); API5 BFLA (admin endpoints with non-admin token); API6 Sensitive Business Flows (replay, race); API7 SSRF (URL inputs); API8 Misconfig (info disclosure via stack traces); API9 Inventory Management (hidden/deprecated endpoints); API10 Unsafe Consumption.
2. OWASP Top 10 (2021) coverage if Web UI present.
3. WCAG 2.2 coverage if UI present (`@security-a11y`-tagged `test(...)`, `@axe-core/playwright` scan).
4. Negative auth: per critical path, ≥ 1 unauthorized + 1 wrong-role `test(...)`?
5. Input validation: boundary + injection canary (SQL/XSS/SSRF) for free-text fields?

Part B — Framework Hygiene:
6. Hardcoded credentials: grep source for credential-looking strings, basic auth, bearer prefixes. Flag as CRITICAL.
7. Env var usage: sensitive config from `process.env['API_BASE_URL']` / `process.env['API_TOKEN']`, not from hardcoded constants?
8. Logging leaks: any `console.log` / `console.error` that echoes a token, an `Authorization` header, or a secret-bearing response body?
9. Dependency CVEs: check `package-lock.json` vs known CVEs for `@playwright/test`, `@axe-core/playwright`, `typescript`, `eslint`. Cite NVD / GHSA URL.
10. Test data sanitization: `@faker-js/faker`-generated (not real PII), no real customer emails / cards.
11. Failure artifacts: Playwright trace, HTML report, and `--save-har` capture the `Authorization` header and full response bodies — confirm they stay in the run's evidence area and are never pasted into a public channel.
12. Git history: any commit added a credential and a later commit removed it (still in history)?

## Output

- `reports/reviews/security.md` with:
  - Verdict: PASS | PASS-WITH-CHANGES | FAIL.
  - Part A — Security Test Coverage table (per OWASP item: Applicable? / Tests / Verdict COVERED|PARTIAL|MISSING).
  - WCAG verdict if UI present.
  - Part B — Framework Hygiene Findings sorted Critical / High / Medium with `file:line` + 1-line fix suggestion.
  - Bugs Discovered During Review (BUG-NNN + severity).
  - Recommended Next Action.

## Example

OWASP-API coverage row + a hygiene finding in `reports/reviews/security.md`:

```markdown
### Part A — Security Test Coverage
| OWASP item | Applicable | Tests | Verdict |
|---|---|---|---|
| API1:2023 BOLA | yes | tests/api/orders.spec.ts → switch order owner id, expect 403 | COVERED |
| API5:2023 BFLA | yes | — | MISSING |

### Part B — Hygiene (Critical)
- `tests/api/orders-api-client.ts:8` — hardcoded JWT literal "<jwt>". Read it from `process.env['API_TOKEN']`.
```
