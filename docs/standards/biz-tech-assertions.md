# BIZ/TECH Assertion Descriptors

Status: active

Canonical definition of the business/technical assertion-descriptor
convention. Skills reference this file instead of restating the rule, so
the convention has a single source of truth. Normative for the
implementer and reviewer roles across all providers.

Cross-references: `docs/standards/qa-standards.md`,
`docs/bug-aware-policy.md`, `scripts/assertion-guard.py`.

## 1. The descriptor rule

Every assertion carries a human-readable descriptor that names *what kind*
of expectation it encodes, tagged `BIZ:` or `TECH:`.

- **BIZ:** — a business-rule expectation. It encodes a requirement clause:
  what the product must do for the user. Example: `BIZ: a new account
  starts with a zero balance`.
- **TECH:** — a technical/contract expectation. It encodes protocol,
  schema, or infrastructure behaviour independent of business meaning.
  Example: `TECH: response is 201 with Content-Type application/json`.

Both kinds are first-class. A scenario is under-tested if it asserts only
TECH (status/shape) and never BIZ (the actual rule), or vice versa.

## 2. How it is written

Descriptors attach to soft assertions so one scenario reports every
violation in a single run, not just the first.

- Java / AssertJ: `.as("BIZ: ...")` / `.as("TECH: ...")` on every
  assertion, using the per-scenario `world.softly()` (never a fresh
  `new SoftAssertions()` inside a step).
- TypeScript / Playwright: a leading `// BIZ:` / `// TECH:` comment plus a
  soft-assertion call (`expect.soft(...)`).
- Cross-cutting checks (status, content-type, response-time, error shape)
  go through the shared helpers (`HttpAsserts`, `JsonSchemas`) which carry
  their own descriptor argument.

## 3. Review expectations

A reviewer treats a missing or mislabelled descriptor as a finding:

- Every assertion has exactly one `BIZ:` or `TECH:` descriptor.
- At least one `BIZ:` assertion exists per critical scenario.
- Descriptors are specific (`BIZ: balance is debited by the exact amount`),
  not generic (`BIZ: works`).
- A descriptor must not be weakened to mask an app bug — that is a
  bug-aware-policy violation (`docs/bug-aware-policy.md` §6), not a style
  nit.
