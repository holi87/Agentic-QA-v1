#!/usr/bin/env bash
# Standalone runner for an Agentic-OS-generated Playwright + TypeScript suite.
#
# Self-contained: this directory is a complete npm project. A human copies it to
# any machine with Node.js (LTS) installed and runs ./run-tests.sh — no Agentic
# OS required (issue #369).
#
# SUT URLs come from the environment; export them before running:
#   API_BASE_URL=https://sut.example.com/api   (api/*.spec.ts)
#   UI_BASE_URL=https://sut.example.com         (ui/*.spec.ts)
# Optional credentials/secret env vars are documented per-spec in their header
# comments. Specs self-skip when a prerequisite env var is absent.
#
# Pass-through args reach `playwright test`, e.g.:
#   ./run-tests.sh api/                 # only the API specs (path filter)
#   ./run-tests.sh --grep @smoke        # tag filter
#
# Exit code is Playwright's: 0 = all passed, 1 = test failures.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: Node.js (LTS) is required but was not found on PATH." >&2
  echo "       Install from https://nodejs.org/ and re-run." >&2
  exit 2
fi

# Install dependencies (reproducible when a lockfile is present).
if [[ -f package-lock.json ]]; then
  npm ci
else
  npm install
fi

# C2 static gates (issue #364) — fail the build on a lint or type violation
# BEFORE downloading browsers / running tests. ESLint enforces the size/
# structure limits and the hard-wait ban (standards §5–§6); `tsc --noEmit`
# enforces strict typing.
npm run lint
npm run typecheck

# Ensure the Chromium browser the UI specs need is present. On a fresh Linux
# host that also needs OS libraries, run once with sudo:
#   npx playwright install --with-deps chromium
npx playwright install chromium

exec npx playwright test "$@"
