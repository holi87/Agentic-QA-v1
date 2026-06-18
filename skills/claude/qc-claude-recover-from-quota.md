---
name: qc-claude-recover-from-quota
description: "Cross-role recovery when a provider returns a quota / rate-limit signal: record a provider_failover decision, fall back to the next provider, and if the chain is exhausted emit provider_chain_exhausted + a needs_operator_decision chip. Estimate cooldown from rate-limit headers when present."
---

# Skill: qc-claude-recover-from-quota

## Communication

${include_preamble}

## When to use

- A model subprocess returned a quota / rate-limit signal (per the provider-failover detection).
- BEFORE retrying the same provider that just rate-limited.
- NOT for ordinary tool errors (those are handled by the calling skill's retry logic).

## STOP conditions

Halt, emit a `[needs_input]` event, and return `needs_input: <key>` when ANY:
- not running in autonomous mode (an operator is present to decide) → `needs_input: operator_decision`.
- `autonomy.provider_failover=false` in `config/agentic-os.yml` → `needs_input: failover_disabled`.
- no provider chain is configured to fall back to → `needs_input: provider_chain`.

## What to do

1. Record a `provider_failover` decision row: the rate-limited provider, the reason, and the next provider in the chain.
2. Fall back to the next configured provider and resume the interrupted role.
3. If the rate-limit response carries a `Retry-After` (or reset) header, estimate the cooldown and store it on the decision row.
4. If all providers in the chain are cold, do NOT loop: emit a `provider_chain_exhausted` event and a `needs_operator_decision` dashboard chip, then yield.
5. Never log the raw credential or the response body of the rate-limit error.

## Output

- A `provider_failover` decision row (rate-limited provider, next provider, estimated cooldown).
- On exhaustion: a `provider_chain_exhausted` event + `needs_operator_decision` chip.

## Example

The decision row this skill writes. Parses as JSON:

```json
{
  "kind": "provider_failover",
  "from_provider": "claude",
  "to_provider": "codex",
  "reason": "rate_limited",
  "cooldown_seconds": 60,
  "actor": "recover-from-quota"
}
```
