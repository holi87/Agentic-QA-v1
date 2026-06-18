---
name: qc-claude-reviewer-validate-architecture
description: "Dedicated architecture peer-review: read the runtime + CLI contracts and any new modules, then assert the invariants final-gate used to audit inline (no SUT writes, no new mutable globals, no new untyped excepts, dispatch table honored, gates fail-closed). Emits file:line findings."
---

# Skill: qc-claude-reviewer-validate-architecture

## Communication

${include_preamble}

## When to use

- Invoked by `reviewer-final-gate` for the architecture audit slice (30% weight) — this skill extracts what final-gate used to inline.
- After an implementation phase that added or changed runtime modules.
- NOT for test-only changes with no module/architecture impact.

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- `docs/runtime-contract.md` is missing → `needs_input: contracts`.
- `docs/cli-contract.md` is missing → `needs_input: contracts`.
- the diff under review cannot be resolved (no base ref) → `needs_input: diff_base`.

## What to do

1. Read `docs/runtime-contract.md` and `docs/cli-contract.md` to load the invariants.
2. Enumerate new / changed modules in the diff.
3. Assert, with `file:line` citations, that the change introduces:
   - no writes into the SUT tree (any path under `sut.root`);
   - no new mutable module-level globals;
   - no new bare/broad `except:` without a typed exception + re-raise or logged handling;
   - no bypass of the orchestrator dispatch table (direct provider calls outside the wrapper);
   - no gate that fails open (every new gate defaults to blocking on error).
4. For each violation, cite `file:line` and the contract clause it breaks.
5. Return a verdict the final-gate folds into its architecture dimension.

## Output

- `reports/reviews/architecture.md` with:
  - Verdict: PASS | PASS-WITH-CHANGES | FAIL.
  - Findings table: `file:line` — invariant broken — 1-line fix.
  - Contract clauses checked (runtime-contract / cli-contract section refs).

## Example

A findings block in `reports/reviews/architecture.md`:

```markdown
Verdict: PASS-WITH-CHANGES

| file:line | Invariant | Fix |
|---|---|---|
| agentic_os/runner.py:88 | new bare `except:` swallows errors (runtime-contract §4) | catch `subprocess.CalledProcessError`, log, re-raise |
| agentic_os/state.py:12 | new mutable global `CACHE = {}` | inject via the orchestrator context instead |
```
