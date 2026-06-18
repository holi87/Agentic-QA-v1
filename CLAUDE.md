# CLAUDE.md

Instructions for Claude Code working in the AgenticOS Lab repository.
Full agent guidance: [`AGENTS.md`](AGENTS.md). This file repeats the
hardest rules because Claude Code reads `CLAUDE.md` first.

**English-only — no `_pl` twin.** `CLAUDE.md` (and `AGENTS.md`) are
agent-facing contracts; the canonical and only version lives here.
Do not create `CLAUDE_pl.md`.

## HARD RULES — git workflow

**Branch `main` is read-only for Claude.** FORBIDDEN:

- `git push` (or `--force`) to `main` / `origin/main`;
- `git merge` of any branch into `main` locally;
- `git commit` directly on `main`;
- `git rebase` `main` onto anything;
- `--no-verify`, `--no-gpg-sign`, or any other skipping of hooks/signatures.

Integration into `main` happens exclusively through a Pull Request after
review. Claude may open a PR (`gh pr create`) and add comments. A human
performs the merge.

## Branch per task

Each task = its own branch. Convention:

- `task/<short-desc>` — regular tasks;
- `phase/NN-<short-desc>` — roadmap phases.

Do not bundle multiple tasks into a single branch, even if they are small.

## Before EVERY task starts — REBASE OFF FRESH MAIN

```bash
# 0. Stash local work if any
git status
git stash push -u -m "wip-before-task-<slug>"   # only when something is dirty

# 1. Pull the current main
git fetch origin main
git switch main
git pull --ff-only origin main

# 2. Create a fresh task branch
git switch -c task/<short-desc>
```

If `git pull --ff-only` refuses (local divergence on `main`), stop and
report. Do not resolve with `git reset --hard` or any other destructive
move without the operator's explicit consent.

## After the task

```bash
git status
git push -u origin task/<short-desc>
gh pr create --base main --head task/<short-desc> --title "..." --body "..."
```

**The PR body MUST close the issues it resolves.** Add one `Closes #NN`
line per fully-resolved issue (`Fixes`/`Resolves` work too) so merging
into `main` auto-closes them — keywords take no comma list, one line
each. Use `Refs #NN` for partial work and for epic/parent issues (never
`Closes` a parent from a child PR).

A task is ready for merge only after:

- local validation described in the task / phase file;
- a clean `git status` on the branch;
- review gate (if required);
- an open PR against `main`;
- green CI (if it exists).

**A human performs the merge through the GitHub UI or `gh pr merge`.**
Claude never executes a merge into `main`.

## Parallel agents

The OS may fan out **independent** runtime work (planner probes, implementer
test-families, triage) using the pattern **fan-out → synthesize/dedup barrier
→ single artifact**, with **no shared mutable state**. Correctness-critical
steps NEVER run in parallel: review/final gates, patch approval, and SQLite
(WAL) writes are **single-writer, serialized**. Full doctrine:
[`AGENTS.md`](AGENTS.md) § "Parallel agent orchestration".

## Everything else

Full operating rules, model roles, bug-aware policies and contracts: see
[`AGENTS.md`](AGENTS.md).
