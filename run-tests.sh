#!/usr/bin/env bash
# Agentic OS mandatory runner.
#
# Contract:
# - exit 0 only when tests pass and reports are finalized;
# - exit 1 for product/test failures, including known bugs that remain red;
# - exit 2 for infrastructure or report-generation failures;
# - always refresh reports/ before returning non-zero.
set -uo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
cd "$PROJECT_ROOT" || exit 2

usage() {
  sed -n '2,15p' "$0" | sed 's/^# //; s/^#//'
}

arg=""
self_check_known_bug=0
include_browser=0
for a in "$@"; do
  case "$a" in
    --help|-h) usage; exit 0 ;;
    --self-check-known-bug) self_check_known_bug=1 ;;
    --browser) include_browser=1 ;;
    *) arg="$a" ;;
  esac
done

echo ">>> Agentic OS runner"
echo "    project root: $PROJECT_ROOT"
echo "    arg:          ${arg:-<default>}"

run_tests() {
  rm -rf build/test-results/test build/reports/tests/test build/reports/cucumber build/reports/allure-report
  if (( self_check_known_bug )); then
    mkdir -p build/test-results/test build/reports/cucumber build/reports/tests/test build/reports/allure-report
    cat > build/test-results/test/TEST-known-bug.xml <<'EOF'
<testsuite name="known-bug" tests="1" failures="1" errors="0" skipped="0">
  <testcase classname="Known bug feature" name="known bug remains red">
    <failure message="BUG-001 remains red" type="AssertionError">expected red known bug</failure>
  </testcase>
</testsuite>
EOF
    cat > build/reports/cucumber/cucumber.json <<'EOF'
[
  {
    "uri": "features/known-bug.feature",
    "elements": [
      {
        "name": "known bug remains red",
        "line": 1,
        "tags": [
          {"name": "@known-bug"},
          {"name": "@bug-001"},
          {"name": "@functional-orders"},
          {"name": "@regression"}
        ]
      }
    ]
  }
]
EOF
    printf '<html><body>known bug remains red</body></html>\n' > build/reports/tests/test/index.html
    printf '<html><body>allure placeholder</body></html>\n' > build/reports/allure-report/index.html
    return 1
  fi

  if [[ -x ./gradlew ]]; then
    case "$arg" in
      "")
        ./gradlew clean test allureReport --info
        ;;
      unit)
        ./gradlew clean unitTest --info
        ;;
      check)
        ./gradlew clean check allureReport --info
        ;;
      *)
        ./gradlew clean test "-Dcucumber.filter.tags=$arg" allureReport --info
        ;;
    esac
    return $?
  fi

  local pybin="${PYTHON:-python3}"
  if [[ -z "${PYTHON:-}" ]] && [[ -x .venv/bin/python ]]; then
    pybin=".venv/bin/python"
  fi
  if ! command -v "$pybin" >/dev/null 2>&1; then
    echo "ERROR: python3 not found" >&2
    return 2
  fi
  mkdir -p build/test-results/test
  local marker_args=()
  if (( include_browser )); then
    # `addopts` in pyproject.toml defaults to ``-m 'not browser'``; passing
    # an explicit selector overrides it so the opt-in group runs alongside
    # the default suite (issue #135).
    marker_args=(-m "browser or not browser")
  fi
  # macOS default Bash 3.2 raises `unbound variable` when expanding an
  # empty array under `set -u`; the `+` form keeps the expansion empty
  # without tripping nounset (issue #183).
  "$pybin" -m pytest tests ${marker_args[@]+"${marker_args[@]}"} --junitxml build/test-results/test/agentic-os-pytest.xml
}

finalize_reports() {
  local rc=0
  if [[ -x scripts/copy-reports.sh ]]; then
    scripts/copy-reports.sh --clean || rc=2
  else
    echo "ERROR: scripts/copy-reports.sh missing" >&2
    rc=2
  fi

  if [[ -x scripts/extract-last-run.sh ]]; then
    scripts/extract-last-run.sh || rc=2
  else
    echo "ERROR: scripts/extract-last-run.sh missing" >&2
    rc=2
  fi

  if [[ -x scripts/build-summary.sh ]]; then
    scripts/build-summary.sh || rc=2
  else
    echo "ERROR: scripts/build-summary.sh missing" >&2
    rc=2
  fi

  [[ -f reports/last-run.json ]] || rc=2
  [[ -f reports/summary.md ]] || rc=2
  return "$rc"
}

run_tests
test_rc=$?

finalize_reports
reports_rc=$?
if (( reports_rc != 0 )); then
  echo "ERROR: report generation failed; returning infra exit 2" >&2
  exit 2
fi

case "$test_rc" in
  0)
    echo ">>> Done: tests green and reports refreshed."
    exit 0
    ;;
  1)
    echo ">>> Done: product/test failures remain red; reports refreshed."
    exit 1
    ;;
  130)
    echo ">>> Interrupted by operator."
    exit 130
    ;;
  2|3|4|5)
    echo "ERROR: infrastructure/test-runner failure exit=$test_rc" >&2
    exit 2
    ;;
  *)
    echo "ERROR: unknown test-runner exit=$test_rc" >&2
    exit 2
    ;;
esac
