#!/usr/bin/env bash
# Agentic OS thin shim.
# Detects repo root, sets PYTHONPATH, dispatches to python -m agentic_os.
# Must not contain workflow logic. Propagates Python exit code verbatim.
set -u

resolve_repo_root() {
  if command -v git >/dev/null 2>&1; then
    if root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
      printf '%s\n' "$root"
      return 0
    fi
  fi
  local self
  self="${BASH_SOURCE[0]:-$0}"
  cd "$(dirname "$self")/.." >/dev/null 2>&1 || return 1
  pwd
}

REPO_ROOT="$(resolve_repo_root)" || {
  printf 'error: cannot resolve repo root\n' >&2
  exit 2
}

PYBIN="${PYTHON:-python3}"
if ! command -v "$PYBIN" >/dev/null 2>&1; then
  printf 'error: %s not found in PATH\n' "$PYBIN" >&2
  exit 2
fi

export PYTHONPATH="$REPO_ROOT/scripts/agentic-os${PYTHONPATH:+:$PYTHONPATH}"

cd "$REPO_ROOT" || exit 2
exec "$PYBIN" -m agentic_os "$@"
