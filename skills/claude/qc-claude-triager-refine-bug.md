---
name: qc-claude-triager-refine-bug
description: "Improve a specific bug entry — clarify steps to reproduce, add missing evidence pointers (trace, screenshot, HAR), suggest reproduction commands (curl, `npx playwright test --grep @bug-NNN`). Use when a bug file was written under time pressure with TBD placeholders or vague descriptions."
---

# Skill: qc-claude-triager-refine-bug

## Communication

${include_preamble}

## Standards

This skill operates under shared conventions — read the canonical source, do not restate it:

- Generated-test structure & idioms (Playwright + TypeScript) — `docs/standards/playwright-ts-standards.md` (§1 layering, §5 assertions/web-first waiting, §7 tags, §8 security).
- Bug report fields, evidence layout, `scenario:` pointer format — `docs/standards/bug-reporting.md`.

## When to use

- After first-check or implementer verify created a bug skeleton with TBD fields.
- Before submission to ensure each bug is independently reproducible by a third party.
- When a reviewer comment flags a bug as 'not actionable' or 'reproduction unclear'.
- NOT for new failures (use first-check).
- NOT for changing severity / priority (use severity-priority).

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- the target `bugs/BUG-NNN-<slug>.md` does not exist → `needs_input: bug_file`.
- the referenced `scenario:` spec test (`tests/<area>.spec.ts::<title>`) cannot be located → `needs_input: scenario`.
- no evidence exists and none can be captured (annotate `TBD — manual capture needed`) → `needs_input: evidence`.

## What to do

1. Read the target `bugs/BUG-NNN-<slug>.md` (frontmatter + body).
2. Identify gaps:
   - TBD placeholders in any section.
   - Vague Steps to Reproduce (missing prerequisite state, missing exact payload, missing user role).
   - Missing Expected (per spec) citation (no link to OpenAPI path, requirements section, or screenshot ref).
   - Missing Actual evidence (no error message, no stack trace head, no response body excerpt, no screenshot).
   - Missing reproduction command (curl invocation for API, `npx playwright test --grep @bug-NNN` for the tagged spec).
3. Read related artifacts: the spec file at the `scenario:` ref, its page object / typed API client via grep, the OpenAPI/spec section, `requirements.md`.
4. Rewrite Steps to Reproduce with explicit prerequisites:
   - Starting state: exact preconditions (auth state, seed data, env vars).
   - Action: exact step (verb + object + payload).
   - Observable: exact result (status code, response body field, UI locator state).
5. Add reproduction command block (`bash` fence):
   ```
   # API repro
   curl -X POST $API_BASE_URL/path -H 'Authorization: Bearer $TOKEN' -d '{"k": "v"}'
   # Playwright repro (the spec tagged @bug-NNN)
   npx playwright test --grep @bug-NNN
   ```
6. Add Evidence pointers — the Playwright failure artifacts: `evidence/BUG-NNN/trace.zip`, `evidence/BUG-NNN/screenshot.png`, `evidence/BUG-NNN/network.har`, `evidence/BUG-NNN/response.json`. If missing, capture or note `TBD — manual capture needed`. Treat traces/HAR as sensitive (they carry the `Authorization` header) — keep them in the run's evidence area, never paste into a public channel (standards §8).
7. Add Suggested Fix (one sentence): root-cause hypothesis or pointer to spec section being violated.
8. Re-cite Expected (per spec) with exact OpenAPI `path` + status code OR requirements.md line ref OR WCAG criterion.
9. Append `## Refinement History` row: ISO timestamp + 1-line summary of what was clarified.
10. Run `./scripts/new-bug.sh --reindex` to refresh `bugs/README.md` if frontmatter changed.
11. Commit: `git add bugs/BUG-NNN-*.md evidence/BUG-NNN/ && git commit -m 'docs: refine BUG-NNN reproduction steps + evidence'`.

## Output

- Fully refined `bugs/BUG-NNN-<slug>.md` — no TBD placeholders in required sections (Steps, Expected, Actual, Evidence, Impact, Suggested Fix).
- Reproduction command block present.
- Evidence pointers concrete (or explicit `TBD — manual capture needed` annotation with reason).
- `## Refinement History` row appended.
- Refreshed `bugs/README.md` index if frontmatter changed.
- Git commit `docs: refine BUG-NNN reproduction steps + evidence`.

## Example

A refined reproduction block (the shape this skill produces — explicit state, command, expected vs actual):

```bash
# Repro for BUG-014 (refined)
# Starting state: paid order #42 owned by the authenticated customer
curl -X POST "$API_BASE_URL/api/orders/42/cancel" -H "Authorization: Bearer $TOKEN"
# Expected: 409 per requirements.md §4.2 — Actual: 200
# Re-run the tagged spec: npx playwright test --grep @bug-014 (trace + HTML report on failure)
```
