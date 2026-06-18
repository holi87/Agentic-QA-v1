# Cucumber Tag Convention

Status: active

QualityCat uses a layered tag scheme. `QC-claude-init` decides project-specific
business-area tags interactively. Lifecycle tags below are mandatory.

## Mandatory Lifecycle Tags

These exist on every project and have dedicated runners.

| Tag | Purpose | Target Run Time |
|---|---|---|
| `@healthcheck` | Verifies environment / SUT reachability before any real test. Pure infrastructure pulse. | < 10s |
| `@smoke` | Top 3-5 critical happy-paths proving the SUT is alive. | < 60s |
| `@critical` | Top business goals — must always pass on a green build. | < 5 min |
| `@regression` | Every scenario gets this tag. Full suite. | depends |
| `@negative` | Documented error responses (401, 403, 404, 422, …). | depends |
| `@boundary` | Numeric / string / date edges per critical input. | depends |
| `@security` | OWASP API Top 10 and OWASP Top 10 mappings. | depends |
| `@extended` | Round 2 — parametric variants, deep coverage. Optional in panic mode. | depends |
| `@known-bug` | Paired with `@bug-NNN`. Scenario expected to fail until app fixed. | depends |

## Project-Specific Business-Area Tags

Decided during `QC-claude-init`. Format: `@functional-<area>` (kebab-case, lower).

Examples: `@functional-auth`, `@functional-users`, `@functional-orders`,
`@functional-billing`, `@functional-search`.

Every scenario MUST have exactly one `@functional-<area>` tag.

## Specialty Sub-Tags

- `@security-a11y` — accessibility checks (WCAG 2.2). Distinct from `@security`.
- `@owasp-api1` … `@owasp-api10` — OWASP API Top 10 specific item mapping.
- `@owasp-a01` … `@owasp-a10` — OWASP Web Top 10 mapping (2021).
- `@bug-NNN` — pairs with `@known-bug`, references file `bugs/BUG-NNN-<slug>.md`.
- `@reference` — reserves a scenario as pattern reference; never executed by
  default runners.

## Required Combinations

Every scenario MUST have:
- exactly one `@functional-<area>` tag,
- AND at least one of: `@smoke`, `@critical`, `@regression`,
- AND `@regression` (covers everything for full suite runs).

When applicable, also add:
- `@negative` per documented error code,
- `@boundary` for numeric/string/date edges,
- `@security` (with `@owasp-*` sub-tag) for security scenarios,
- `@known-bug @bug-NNN` if scenario is pinned to a known defect.

## CLI Examples

```bash
# Run by lifecycle tag
./gradlew test -Dcucumber.filter.tags="@healthcheck"
./gradlew test -Dcucumber.filter.tags="@smoke"
./gradlew test -Dcucumber.filter.tags="@critical and not @extended"

# Run by business area
./gradlew test -Dcucumber.filter.tags="@functional-users"
./gradlew test -Dcucumber.filter.tags="@functional-auth or @functional-users"

# Run security suite
./gradlew test -Dcucumber.filter.tags="@security"

# Run all OWASP API1 (IDOR) scenarios
./gradlew test -Dcucumber.filter.tags="@owasp-api1"

# Skip known bugs (clean green run)
./gradlew test -Dcucumber.filter.tags="not @known-bug"

# Combine
./gradlew test -Dcucumber.filter.tags="@functional-orders and @critical and not @known-bug"
```

## Anti-Patterns

- Tag explosion (50+ unique tags) → consolidate.
- Capital-case typos (`@Smoke`, `@CRITICAL`) → enforce lowercase.
- Multiple `@functional-*` tags on one scenario → split scenarios.
- `@known-bug` without `@bug-NNN` → either remove tag or add real BUG ID.
- `@critical` on every scenario → criticality loses meaning. Reserve for top business goals.

## Last Updated

2026-05-08.
