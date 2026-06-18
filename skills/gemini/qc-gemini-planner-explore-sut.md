---
name: qc-gemini-planner-explore-sut
description: "Manually probe the System Under Test (SUT) — both API and UI — to validate the documented spec, identify candidate bugs, and refine business understanding before spec design. Run between analyze-task and design-features."
---

# Skill: qc-gemini-planner-explore-sut

## Communication

${include_preamble}

## When to use

- T+01:10, after analyze-task.
- SUT URLs available, credentials known.
- BEFORE writing `tests/<area>.spec.ts` files.
- NOT during implementation (exploration time-boxed, no spelunking in crunch).

## What to do

1. Time-box exploration: 20-30 min max.
2. Define 3-5 charters from top-5 critical goals: 'Explore <area> with <approach> to discover <quality>.' For public web tasks, add a route/link-integrity charter and a browser-console/asset charter.
3. API probes via curl. Verify documented status codes match reality. Try negative cases: missing auth, wrong roles, malformed body, oversized payload, SQL/NoSQL injection canaries. Capture response bodies in `evidence/explore-NNN-*.json`.
4. UI probes via Playwright MCP or browser. Click critical flows (login, CRUD, admin). For content sites, crawl at least 10 same-origin links or all visible links if fewer: record HTTP status, title/heading, console errors, broken images, and one screenshot per representative page. Capture screenshots `evidence/explore-NNN-*.png`. Inspect network tab for hidden/undocumented endpoints. Exclude iframe/embedded surfaces when the task says frames are out of scope.
5. Security micro-probe (proof-only, do NOT exploit): IDOR (switch user IDs), mass assignment (extra fields `role: admin`), unauth access (drop token), rate limiting (50× rapid).
6. Spec vs reality diff per documented endpoint/page: matches / ambiguous / contradicts. Log bugs accordingly. Do not stop after 1-2 green checks unless the task explicitly limits scope to exactly those checks.
7. Append `## Exploration Findings` section to `requirements.md`: confirmed behaviors, discovered behaviors, spec contradictions, new scenarios.
8. Commit: `git add evidence/ bugs/ requirements.md STATUS.md && git commit -m 'docs: SUT exploration findings'`.

## Output

- `evidence/explore-NNN-*.{png,json}` — captured probes.
- `bugs/BUG-NNN-<slug>.md` — initial bug entries; `bugs/README.md` index updated.
- `requirements.md` — appended `## Exploration Findings` section.
- `STATUS.md` — exploration timestamp + summary.
- Git commit `docs: SUT exploration findings`.

## Example

A `SUT-DISCOVERY.md` block written into `requirements.md`:

```markdown
## SUT Discovery — orders-api

- Stack: Spring Boot 3 / PostgreSQL (from /actuator/info)
- Auth: Bearer JWT in the Authorization header

| Route | Method | Markers | Notes |
|---|---|---|---|
| /api/orders | GET | @functional-orders | paginated, page size 20 |
| /api/orders/{id} | GET | @functional-orders | 404 on unknown id |
| /api/orders/{id}/cancel | POST | @security @owasp-api5 | mutating — needs decision |
```
