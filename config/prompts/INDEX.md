# Agentic OS — role prompts index

Canonical role prompts (provider-agnostic). The underlying model is
selected per project in `config/agentic-os.yml` under `models.<role>`.

| Role | File | Used by | Default provider |
|---|---|---|---|
| Planner | `planner.md` | `models.planner` | claude (opus) |
| Implementer | `implementer.md` | `models.implementer` | claude (sonnet) |
| Reviewer | `reviewer.md` | `models.reviewer` | codex |
| Triager | `triager.md` | `models.triager` | claude (haiku) → codex → antigravity (gemini-3.1-pro-high) |
| Bug adjudication | `bug-adjudication.md` | planner, on bug tasks | — |

## Skills auto-injected per role

Each role auto-loads skills from `skills/{provider}/` matching schema
`qc-{provider}-{role}-{name}.md`. Toggle enabled/disabled per role via
`config/skills.yml` or the dashboard `/skills` page.

Skill names per role (uniform across providers):

- **planner**: `analyze-task`, `explore-sut`, `design-features`
- **implementer**: `implement-api`, `implement-ui`, `init-project`,
  `package`, `verify`
- **reviewer**: `validate-features`, `validate-tests`,
  `validate-security`, `final-gate`
- **triager**: `first-check`, `severity-priority`, `refine-bug`

## Migration from legacy names

Older repos may reference `opus-planner.md`, `sonnet-implementer.md`,
`codex-reviewer.md`. Renamed files (`planner.md` / `implementer.md` /
`reviewer.md`) carry the same content with provider-neutral framing
and skill references.

## Standards referenced by all prompts

- `docs/bug-aware-policy.md`
- `docs/severity-policy.md`
- `docs/standards/bug-reporting.md`
- `docs/standards/qa-standards.md`
- `docs/standards/playwright-ts-standards.md`
- `docs/standards/cucumber-tags.md`
- `docs/runtime-contract.md`
