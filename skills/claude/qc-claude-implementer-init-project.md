---
name: qc-claude-implementer-init-project
description: "QualityCat project initializer — scaffolds a self-contained Playwright + TypeScript test workspace from the assets this Agentic OS repo ships: the `templates/playwright-ts-framework` scaffold, docs/standards, helper scripts, prompts and enabled skills. Stops instead of guessing when the scaffold template or AGENTIC_OS_HOME is missing."
---

# Skill: qc-claude-implementer-init-project

## Communication

${include_preamble}

## Standards

This skill operates under shared conventions — read the canonical source, do not restate it:

- Generated-code structure & idioms (Playwright + TypeScript) — `docs/standards/playwright-ts-standards.md` (§1 layering, §6 size limits, §7 tags, §8 security). The scaffold layout (`package.json`, `playwright.config.ts`, `tsconfig.json`, `tests/api`, `tests/ui`) materializes these idioms.
- Tag families (carried over to Playwright `{ tag: [...] }`) — `docs/standards/cucumber-tags.md`.

## When to use

- First action when starting any new project that needs automated tests.
- Fresh empty directory (or only brief / docs from stakeholders).
- NOT if directory already initialized (`.git` or `package.json` present).

## What to do

1. Resolve `AGENTIC_OS_HOME` (Agentic OS root): must be exported by operator (or passed `--agentic-os-home <path>`). STOP if unset. Verify it contains `scripts/agentic-os.sh`, `scripts/agentic-os/`, `scripts/agentic-os/templates/playwright-ts-framework/`, `skills/`, `config/prompts/`, `docs/standards/`, `run-tests.sh`, and helper scripts `scripts/{new-bug,copy-reports,extract-last-run,build-summary}.sh`.
2. Detect environment via `pwd`, `ls -la`. STOP if `.git` exists or `package.json` present. Ask user before overwriting.
3. Decide business-area tags: parse `--areas` flag or prompt user (`AskUserQuestion`). Normalize to kebab-case lowercase → `@functional-<area>` tags.
4. Create the directories the layout needs: `bugs/`, `reports/`, `evidence/`, `qualitycat-standards/`, `solution/`, `scripts/`, `tests/`, `tests/api/`, `tests/ui/` (specs live under `tests/api/*.spec.ts` and `tests/ui/*.spec.ts`).
5. Write seed docs if missing: `STATUS.md`, `requirements.md`, `MCP_INVENTORY.md`, `IMPLEMENTATION_PROGRESS.md`, `SUBMISSION_CHECKLIST.md`, `TAG_PLAN.md`, `solution/ARCHITECTURE.md`, `solution/README.md`, `bugs/README.md`.
6. Copy frozen standards from `$AGENTIC_OS_HOME/docs/standards/{qa-standards,playwright-ts-standards,bug-reporting,cucumber-tags}.md` → `qualitycat-standards/` (the `cucumber-tags.md` tag families carry over to Playwright selection).
7. Copy helper scripts to project root: `run-tests.sh` and `scripts/{new-bug,copy-reports,extract-last-run,build-summary}.sh`; preserve executable bits.
8. Vendor metadata under `.qualitycat/agentic-os/` only: selected skills, `config/prompts/`, `docs/standards/`, and `VERSION.txt` with source git rev + timestamp. Do not reference removed legacy skill directories or root-level standards.
9. Scaffold the framework: copy the shipped `$AGENTIC_OS_HOME/scripts/agentic-os/templates/playwright-ts-framework/` files into the project root — `package.json`, `playwright.config.ts`, `tsconfig.json`, `eslint.config.js`, `run-tests.sh`, `.gitignore`. If a partial scaffold from an interrupted prior run is present (e.g. `playwright.config.ts` but no `package.json`), wire the missing files in without overwriting the present config. STOP with `needs_input: test_stack` only if the scaffold template is absent.
10. Verify only commands that exist: `./run-tests.sh --help`; install deps (`npm ci` when `package-lock.json` is present, else `npm install`) then `npm run typecheck` (`tsc --noEmit`), `npm run lint`, and a no-SUT discovery run `npx playwright test --list`. STOP on any failure.
11. Git init: `git init -b main && git add . && git commit -m 'chore: init project structure'`.
12. Optional `--origin <url>`: `git remote add origin <url> && git push -u origin main`. On failure → STOP, defer remote in STATUS.md.

## Output

- Self-contained project layout: contest deliverables at root (`solution/`, `bugs/`, `reports/`, `evidence/`, `tests/`, `run-tests.sh`).
- Playwright + TypeScript scaffold at root: `package.json`, `playwright.config.ts`, `tsconfig.json`, `eslint.config.js` — copied from the shipped template, never hand-faked.
- `qualitycat-standards/` (4 frozen files, including `playwright-ts-standards.md`).
- `.qualitycat/agentic-os/{skills,prompts,standards}/` + `VERSION.txt`.
- Healthcheck: `tsc --noEmit` + lint + `npx playwright test --list` PASS, or `needs_input: test_stack`.
- Git: `main` branch, first commit `chore: init project structure`.

## Example

A `package.json` seed for the test stack, copied from the shipped scaffold. Parses as JSON:

```json
{
  "name": "agentic-os-generated-suite",
  "private": true,
  "type": "module",
  "scripts": {
    "test": "playwright test",
    "lint": "eslint .",
    "typecheck": "tsc --noEmit",
    "report": "playwright show-report"
  },
  "devDependencies": {
    "@playwright/test": "^1.49.0",
    "@types/node": "^20.0.0",
    "typescript": "^5.4.0"
  },
  "dependencies": {
    "ajv": "^8.17.0"
  }
}
```
