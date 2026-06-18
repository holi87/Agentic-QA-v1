---
name: qc-gemini-triager-escalate-blocker
description: "Route a critical finding: when a bug is S1+P1 or ≥3 unmatched failures point at one component, mark escalation_required, emit an escalation event with a suggested owner (from CODEOWNERS), and surface a dashboard chip. Always interrupt for S1."
---

# Skill: qc-gemini-triager-escalate-blocker

## Communication

${include_preamble}

## When to use

- A newly logged bug carries S1 + P1.
- OR ≥ 3 unmatched failures within one run point at the same component.
- NOT for S2 or lower (those file normally without interrupting the operator).

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- `docs/severity-policy.md` is missing (cannot confirm S1) → `needs_input: severity_policy`.
- no S1 candidate and no ≥3-failure component cluster exists → `needs_input: escalation_trigger`.
- the bug file to annotate does not exist → `needs_input: bug_file`.

## What to do

1. Confirm the trigger: bug is S1 + P1 per `docs/severity-policy.md`, OR ≥ 3 unmatched failures share a `component`.
2. Set `escalation_required: true` in the bug frontmatter.
3. Resolve a suggested owner from `CODEOWNERS` (match the failing path) if present; else leave `owner: unassigned`.
4. Emit an `escalation` event (NDJSON) with `bug_id`, `severity`, `component`, `suggested_owner`.
5. Surface a dashboard chip via the event so the operator sees it without digging into the DB.
6. Always interrupt for S1 (the operator-interruption budget does not suppress S1).

## Output

- Bug frontmatter updated with `escalation_required: true` + `owner`.
- One NDJSON `escalation` event appended to the run event log.
- Dashboard chip surfaced for the escalated bug.

## Example

The frontmatter update + event this skill emits. The first block parses as YAML, the second as JSON:

```yaml
escalation_required: true
severity: S1
priority: P1
component: API
owner: payments-team
```

```json
{"event": "escalation", "bug_id": "BUG-019", "severity": "S1", "component": "API", "suggested_owner": "payments-team"}
```
