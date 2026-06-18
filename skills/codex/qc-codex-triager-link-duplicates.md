---
name: qc-codex-triager-link-duplicates
description: "Near-duplicate fold: after first-check returns NO-MATCH, search existing bugs by stack-trace fingerprint, endpoint+assertion match, and OWASP+status before opening a new bug. On a hit, append @known-bug @bug-NNN to the scenario instead of proliferating bugs."
---

# Skill: qc-codex-triager-link-duplicates

## Communication

${include_preamble}

## When to use

- After `triager-first-check` produces a NO-MATCH classification, BEFORE opening a new bug.
- When the `bugs/` backlog is large enough that duplicates are likely.
- NOT before first-check has run (no classification to act on).

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- the `bugs/` directory is missing or empty (nothing to compare against) → `needs_input: bugs`.
- the failure has no stack trace, endpoint, or assertion text to fingerprint → `needs_input: failure_evidence`.
- `reports/last-run.json` is missing → `needs_input: test_run`.

## What to do

1. Build a fingerprint of the NO-MATCH failure: (a) stack-trace head, (b) endpoint + assertion text, (c) OWASP item + status code.
2. Compare against every `bugs/BUG-NNN-*.md`:
   - (a) stack-trace fingerprint within Levenshtein ≤ 5;
   - (b) endpoint + assertion text exact match;
   - (c) same OWASP item AND same status code.
3. On a hit by any rule → DUPLICATE: append `@known-bug @bug-NNN` to the failing scenario; do NOT create a new bug. Record the matched rule.
4. On no hit → NEW_DISTINCT: hand back to first-check / new-bug creation.
5. Reviewer still bites: `validate-features` rejects an orphan `@known-bug` tag, so only append when the matched `bugs/BUG-NNN-*.md` exists.

## Output

- Either a DUPLICATE marker (matched `bug_id` + rule, no new bug) or NEW_DISTINCT (proceed to creation).
- A `.spec.ts` diff appending `@known-bug @bug-NNN` on a DUPLICATE.
- A line in the triage audit log recording the decision + rule.

## Example

The decision this skill returns. Parses as JSON:

```json
{
  "result": "DUPLICATE",
  "matched_bug": "BUG-014",
  "rule": "endpoint+assertion-exact",
  "action": "append @known-bug @bug-014 to tests/orders.spec.ts:18",
  "new_bug_created": false
}
```
