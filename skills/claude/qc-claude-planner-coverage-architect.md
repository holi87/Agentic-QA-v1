---
name: qc-claude-planner-coverage-architect
description: "Autonomous coverage architect. Discovers SUT inventory, classifies each candidate per ISO 25010 + OWASP + business area, and proposes generate_now only for the autonomous-safe bucket. Issue #229."
---

# Skill: qc-claude-planner-coverage-architect

## Communication

- Mode: caveman (drop articles/filler/pleasantries/hedging, fragments OK).
- Code, commits, security warnings: write normal.
- Errors: quoted exact.
- Output language: English.

## When to use

- ONLY when `autonomy.coverage_architect=true` in `config/agentic-os.yml`.
  Operator-gated. Off by default.
- Invoked by the orchestrator after `analyze_work_item` finishes, before
  the operator review prompt fires.
- Skipped otherwise — the existing operator-driven candidate decision
  flow remains the default.

## What to do

1. **Discover SUT inventory** in this order, accept the first that yields
   ≥ 1 endpoint:
   - `sitemap.xml` at the configured `sut.web` root.
   - `robots.txt` (read `Sitemap:` directives, follow them).
   - OpenAPI / Swagger spec under `docs/api/` or `sut.api.openapi_path`
     if configured.
   - Service route table dumped under `agentic-os-runtime/analysis/<id>/`.
   - Fallback: HEAD/GET the homepage with depth-1 link crawl, capped at
     20 anchors, same-host only.
2. **Classify each discovered endpoint** by:
   - ISO 25010 dimension (Functional Suitability / Performance Efficiency
     / Compatibility / Usability / Reliability / Security / Maintainability
     / Portability) — pick the *primary* dimension that drives the test.
   - OWASP API or Web Top-10 item (e.g. `API1:2023` for BOLA, `A03:2021`
     for injection). Skip when the endpoint is purely informational.
   - Business-area label sourced from the task spec (e.g. `orders`,
     `auth`, `catalog`).
3. **Propose candidates**. For each, set `decision`:
   - `generate_now` ONLY when ALL hold:
     - `test_type='ui'` AND target is navigational/read-only (no form
       POST, no auth mutation); OR
     - `test_type='api'` AND HTTP method ∈ {GET, HEAD, OPTIONS} AND path
       matches a documented spec (OpenAPI hit) OR is sitemap-discovered;
       OR
     - the candidate is a coverage-floor companion (a11y / console /
       network / link-walk — see UI generator child #230).
   - `needs_operator_decision` for EVERYTHING ELSE — mutating methods,
     auth-changing flows, form pages (`/new` / `/create` / `/edit` /
     `/login` / `/signup` / `/register`), security probes, payment flows.
4. **Write a decisions row** for each promoted candidate via the
   orchestrator's `decisions` insert:
   - `actor='planner-autopilot'`
   - `rationale` cites the discovery source (e.g. `sitemap.xml: /orders`)
     and the rule (e.g. `api-read-only:GET`).
5. **Hand off** to the existing `analyze_work_item` flow. The Python
   helper `_apply_coverage_architect` in `agentic_os/analysis.py`
   enforces the same rule programmatically — your job is the discovery +
   classification narrative the operator audits in
   `analysis/<id>/candidate-tests.md`.

## Hard rules (the reviewer still bites)

- NEVER promote a mutating endpoint (`POST` / `PUT` / `PATCH` / `DELETE`)
  to `generate_now` — even when the response is documented.
- NEVER promote a UI flow whose target path matches a form hint.
- The reviewer skill `qc-claude-reviewer-validate-tests` Hard rule #2
  (assertion weakening) remains untouched — this skill only writes plan
  rows; the reviewer rejects bad assertions regardless of who proposed
  them.
- Every promoted candidate carries the `planner-autopilot` actor in the
  dashboard `/candidates` view so the operator can override without
  digging into the database.

## Output

- Append a "Coverage Architect" section to
  `analysis/<work_item_id>/candidate-tests.md` listing:
  - Discovery source(s) used.
  - Promoted candidates table (candidate_id, target, rule, OWASP item,
    business area).
  - Items that stayed `needs_operator_decision` (and why).
- The Python autopilot writes `summary.planner_autopilot_flipped: N`
  for dashboard surfacing.

## Reference

- Auto-decision Python helper: `agentic_os.analysis._apply_coverage_architect`.
- Config flag: `autonomy.coverage_architect` in `config/agentic-os.yml`.
- Marker contract for downstream generators: see
  `qc-claude-reviewer-validate-tests` step 12 (issue #233).

## Example

The "Coverage Architect" section appended to `candidate-tests.md`:

```markdown
## Coverage Architect

Discovery source: sitemap.xml (12 urls) + OpenAPI docs/api/orders.yaml

| candidate_id | target | rule | OWASP | area | decision |
|---|---|---|---|---|---|
| cand-011 | GET /api/orders | api-read-only:GET | — | orders | generate_now |
| cand-012 | POST /api/orders/{id}/cancel | mutating-method | API5:2023 | orders | needs_operator_decision |
```
