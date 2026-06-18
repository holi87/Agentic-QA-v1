---
name: qc-gemini-planner-analyze-task
description: "Read session task brief plus provided documentation (PDF/MD/OpenAPI/proto), extract business domain, identify critical user paths, map requirements to ISO 25010 dimensions, write requirements.md and MCP_INVENTORY.md. Stack already fixed — NO stack selection."
---

# Skill: qc-gemini-planner-analyze-task

## Communication

${include_preamble}

## When to use

- T+00:30, after project init.
- Task brief delivered. Documentation files available.
- BEFORE any test design or implementation.
- NOT during implementation (analysis frozen unless brief changes).

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- `requirements.md` is missing or empty → `needs_input: requirements`.
- the task brief names no SUT URL, OpenAPI spec, or repo → `needs_input: sut_target`.
- a candidate would lack `expected_assertion`, `required_test_data`, or `cleanup_strategy` → `needs_input: candidate_metadata`.
- a referenced `decisions` row is not found in the DB → `needs_input: decision`.

## What to do

1. Inventory inputs: `ls -la` of doc drop, list URLs from brief.
2. Read brief slowly. Extract business domain, actors/roles, core entities, critical user goals (3-7 max).
3. Parse API spec if provided. Build endpoint table (method, path, auth, body schema, status codes); top-5 candidate endpoints by business criticality. Respect configured surfaces: if API is disabled, do not convert prose routes into API candidates.
4. For public web or exploratory tasks, derive a breadth map before planning: homepage, navigation links, representative detail pages, feed/RSS, sitemap, robots, broken images, console errors, accessibility basics, and light performance. Target 8-12 candidate checks before pruning.
5. Treat slash-like words from prose as routes only when they are in an in-scope endpoint/page list; ignore routes mentioned only in out-of-scope text.
   - Candidate Quality Contract: every proposed test must name target surface, exact expected assertion, business-visible value, required test data, cleanup/reset strategy, functional area tag, lifecycle tag, and source reference. Do not propose visibility-only, status-only, or duplicate smoke checks as final coverage.
6. Map ISO 25010 per requirement: Functional Suitability / Performance / Security / Reliability / Usability / Maintainability / Portability.
7. Identify business invariants — what MUST always hold (e.g. `total = sum(items.price * items.qty)`, state transitions, authorization rules).
8. Build Business Assertions Matrix per top-5 path: business invariant + technical invariant + Playwright tag.
9. Identify test data strategy: builders vs fixtures, cleanup approach.
10. Build MCP_INVENTORY.md: per-system verdict REQUIRED / OPTIONAL / NONE_REQUIRED / BLOCKED with auth status, env vars, fallback procedure.
11. Flag ambiguities — anything unclear → log as Info-severity bug, document interpretation in Open Questions section.
12. Commit: `git add requirements.md MCP_INVENTORY.md STATUS.md && git commit -m 'feat: capture requirements and external systems inventory'`.

## Output

- `requirements.md` — fully populated (business domain, actors, entities, top-5 goals, API map, public-web breadth map when applicable, Candidate Quality Contract, business invariants, assertions matrix, test data strategy, Playwright tag plan, out of scope, open questions).
- `MCP_INVENTORY.md` — external systems inventory with verdict per system.
- `STATUS.md` — `T+00:30 analyze complete` row with key decisions.
- Git commit `feat: capture requirements and external systems inventory`.

## Example

A single TEST-PLAN.json candidate (the unit this skill emits). Parses as JSON:

```json
{
  "candidate_id": "cand-007",
  "target_path": "/api/orders/{id}/cancel",
  "test_type": "api",
  "expected_assertion": "BIZ: cancelling an already-paid order is refused with 409 and the balance is unchanged",
  "required_test_data": { "order_id": "seeded-paid-order", "actor": "owner-token" },
  "cleanup_strategy": "read-only",
  "decision": "generate_now"
}
```
