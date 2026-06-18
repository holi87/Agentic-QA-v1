#!/usr/bin/env bash
# Generate Cucumber tag coverage matrix for use in test-strategy.md.
# Usage: ./scripts/coverage-matrix.sh [features-dir]
set -euo pipefail

FEATURES_DIR="${1:-src/test/resources/features}"

if [[ ! -d "$FEATURES_DIR" ]]; then
  echo "ERROR: features directory not found: $FEATURES_DIR"
  exit 1
fi

echo "# Cucumber Tag Coverage Matrix"
echo
echo "Source: $FEATURES_DIR"
echo "Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo
echo "## Tag Counts"
echo
echo "| Tag | Scenario Count |"
echo "|---|---|"

# Extract all tags, count occurrences. Tag = @word at line start in .feature.
grep -hoE '@[a-zA-Z0-9_-]+' "$FEATURES_DIR"/*.feature 2>/dev/null \
  | sort \
  | uniq -c \
  | sort -rn \
  | awk '{ printf("| %s | %d |\n", $2, $1) }'

echo
echo "## Feature Files"
echo
echo "| File | Scenario Count |"
echo "|---|---|"

for f in "$FEATURES_DIR"/*.feature; do
  [[ -f "$f" ]] || continue
  count=$(grep -cE '^\s*Scenario(\s|:)' "$f" || true)
  echo "| $(basename "$f") | $count |"
done

echo
echo "## Total"
echo
total=$(grep -hcE '^\s*Scenario(\s|:)' "$FEATURES_DIR"/*.feature 2>/dev/null | awk '{s+=$1} END {print s+0}')
echo "Total scenarios: $total"
