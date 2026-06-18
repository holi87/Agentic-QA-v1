# Skills

Skills are an optional, swappable input to the model prompt. Each skill
is a Markdown file with a YAML frontmatter at the top:

```markdown
---
name: my-skill
description: Short description of when to use it.
---

# Skill: my-skill

## When to use
...

## What to do
...
```

Polish translation: [`README_pl.md`](README_pl.md).

## Structure

```
skills/
├── claude/      # skills for the claude provider (planner/implementer/reviewer/triager)
├── codex/       # skills for the codex provider (planner/implementer/reviewer/triager)
└── gemini/      # skills for the gemini / Antigravity provider
```

The operator can add custom skills through the dashboard (`/skills` →
Install from URL) or drop a file manually into the matching directory.

## Activation

Edit `config/skills.yml` (per-role enabled list) or use the dashboard
toggles. By default every skill is **disabled** — the operator turns on
what is needed deliberately.

## Frontmatter format

| Field         | Required | Description                                       |
|---------------|----------|---------------------------------------------------|
| `name`        | ✅       | Unique name (must match the filename without .md) |
| `description` | ✅       | 1–3 sentences explaining when to use it           |
| `tags`        | ⚪       | List of tags for filtering                        |
| `min_version` | ⚪       | Minimum Agentic OS version (semver)               |

## Security

- A skill must not contain literal secrets (the validator rejects it).
- Path traversal in a skill ID is blocked.
- Skills from an external URL require an operator decision per host.

## Requirements for provider skills (`skills/{provider}/*.md`)

Provider skills assume a specific runtime — without the elements below
they will execute, but the results drift.

### Claude runtime

- **Caveman mode active** — every skill carries a `## Communication`
  block requiring `Mode: caveman` (drop articles/filler/pleasantries,
  fragments OK; code/commits/security: normal). In Claude Code turn on
  `/caveman lite|full|ultra` before the session, or leave the default
  `full` if the operator configured the hooks that way. Without caveman
  tokens are wasted and audits return verbose responses.
- **Output language: English** — every artifact (`requirements.md`,
  `bugs/BUG-NNN-*.md`, `reports/reviews/*.md`, commit messages) is in
  English. Polish exceptions are allowed only in direct operator-to-
  assistant conversation.
- **Subagents OK** — skills allow parallel `Agent` calls for independent
  slices. Requires a model that exposes the `Agent` tool (Claude Code,
  Antigravity).

### Codex runtime

- **Prompt injection, not a global `SKILL.md`** — `skills/codex/*.md`
  files are injected by Agentic OS as prompt fragments. They are not
  `~/.codex/skills/<name>/SKILL.md` bundles, so no `agents/openai.yaml`
  folders here.
- **No recursive Codex invocation** — a skill must not instruct the
  model to run `codex "$(cat skills/codex/...)"`. The runtime already
  delivered the skill body inside the prompt.
- **No Claude-only tools** — Codex skills must not require
  `AskUserQuestion` or `Agent`. When data is missing they must abort
  with a short `needs_input: <field>` and a concrete list of gaps.
- **Output language: English** — artifacts, review reports and commit
  messages stay in English, as with every other provider.

### Project context

- **`AGENTS.md` at the repo root** — hard git workflow contract
  (branch-per-task, PR-only into main, ban on `--no-verify`). The
  provider must have it in context; without this file skills may try to
  write directly into `main`.
- **`CLAUDE.md` (root + per-directory)** — extra project-specific rules.
  Skills DO NOT carry git/PR policy — they rely on CLAUDE.md.
- **`qualitycat-standards/` inside the test project** — copied from
  `docs/standards/{qa-standards,playwright-ts-standards,bug-reporting,cucumber-tags}.md`.
  Skills cite them by name (§N), so a missing file = "standards reference
  not found" error inside the skill.
- **`requirements.md`, `MCP_INVENTORY.md`, `STATUS.md`** — produced by
  `planner-analyze-task` as the first step and consumed by every
  downstream skill. Without them `design-features`, `verify` and
  `final-gate` have no anchor.

### Sandbox / SUT tooling

- **Node + Playwright toolchain** — `npm ci`, `npx playwright test`,
  `npm run lint` / `npm run typecheck` must work. The `implementer-verify`,
  `init-project` and `reviewer-validate-*` skills call them directly.
- **Playwright reporter** — `npx playwright test` must produce the HTML
  report; `verify` and `final-gate` require `playwright-report/`.
- **Helper scripts** — `scripts/copy-reports.sh`,
  `scripts/extract-last-run.sh`, `scripts/new-bug.sh`,
  `scripts/build-summary.sh`, plus `run-tests.sh`. They are copied from
  this Agentic OS repo by `init-project`; without them `verify` and
  `triager-*` have no way to write `reports/last-run.json` or
  `bugs/README.md`.
- **`AGENTIC_OS_HOME` env var** (Agentic OS root) —
  `implementer-init-project` requires an exported `AGENTIC_OS_HOME` pointing
  at the framework repo root (the directory that contains `skills/`,
  `scripts/`, `docs/standards/`, `config/prompts/` and `run-tests.sh`).
  The Python runtime under `scripts/agentic-os/` does **not** read this
  variable; it is a skill-runtime contract enforced by the LLM provider
  when `init-project` runs in a fresh contest project directory. STOP if
  the variable is unset (or the alternative `--agentic-os-home <path>`
  flag is missing).
- **Playwright + MCP / browser** — `planner-explore-sut` and
  `implementer-implement-ui` assume access to Playwright (Java driver
  for the tests; MCP / browser for manual exploration).

### Conventions enforced by skills

**Commit message prefixes** — every skill that runs `git commit` sticks
to this table. The operator should NOT diverge from the convention in
manual commits on the same artifacts.

| Role                       | Prefix    | Example                                                |
|----------------------------|-----------|--------------------------------------------------------|
| planner (artifact)         | `feat:`   | `feat: capture requirements and external systems inventory` |
| planner (exploration)      | `docs:`   | `docs: SUT exploration findings`                       |
| implementer (init/package) | `chore:`  | `chore: init project structure`                        |
| implementer (feature)      | `feat:`   | `feat: implement <area> API tests`                     |
| implementer (test fix)     | `fix:`    | `fix: address test issue in <area>`                    |
| triager (bug edits)        | `docs:`   | `docs: refine BUG-NNN reproduction steps + evidence`   |
| reviewer                   | `docs:`   | `docs: final gate verdict`                             |

**Reviewer report paths** — every review file lives under
`reports/reviews/<role>.md`:

- `reports/reviews/features.md` — output of `reviewer-validate-features`
- `reports/reviews/tests.md` — output of `reviewer-validate-tests`
- `reports/reviews/security.md` — output of `reviewer-validate-security`
- `reports/reviews/final-gate.md` — output of `reviewer-final-gate`

**Skill ordering** — order inside the session loop:

1. `implementer-init-project` (once, at the start)
2. `planner-analyze-task` → `planner-explore-sut` → `planner-design-features`
3. `reviewer-validate-features` (gate before implementation)
4. `implementer-implement-api` / `implementer-implement-ui` (slices)
5. `implementer-verify` (after each slice) — classifies failures inline
6. `reviewer-validate-tests` (after the implementation slices)
7. `reviewer-validate-security` (after round 2)
8. `triager-first-check` — post-hoc sweep after the final `verify`,
   NOT in parallel with it
9. `triager-refine-bug` / `triager-severity-priority` (re-triage)
10. `implementer-package` → `reviewer-final-gate` (BLOCKING before
    submit)

### Missing requirements = symptoms

| Missing                         | What you'll see                                       |
|---------------------------------|-------------------------------------------------------|
| caveman off (Claude)            | Long responses full of filler; PRs grow noisy.        |
| `AGENTIC_OS_HOME` unset              | `init-project` STOP at step 1.                        |
| `AGENTS.md` missing             | The provider may commit straight to `main` locally.   |
| `reports/last-run.json` stale   | `triager-first-check` STOP, demands a re-run.         |
| `qualitycat-standards/` missing | `validate-tests` / `validate-features` flag NO-GO.    |
| Node / Playwright unavailable   | `verify` fails the pre-check, no `playwright-report/`. |
