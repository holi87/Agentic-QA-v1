#!/usr/bin/env bash
# QualityCat — Last Run Extractor
#
# Parses JUnit XML (and Cucumber JSON if present) from the most recent test
# execution and writes a single `reports/last-run.json` summary consumed by
# `/QC-claude-triage-run`. Captures failed scenario metadata: feature file,
# line number, tags, error message, error class, stack-trace head.
#
# Usage:
#   ./scripts/extract-last-run.sh
#   ./scripts/extract-last-run.sh --help
#
# Inputs (auto-detected):
#   build/test-results/test/*.xml            JUnit XML (always present after gradle test)
#   build/reports/cucumber/cucumber.json     Cucumber JSON (if Cucumber HTML plugin enabled)
#
# Output:
#   reports/last-run.json

set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
JUNIT_DIR="$PROJECT_ROOT/build/test-results/test"
CUCUMBER_JSON="$PROJECT_ROOT/build/reports/cucumber/cucumber.json"
OUT_DIR="$PROJECT_ROOT/reports"
OUT_FILE="$OUT_DIR/last-run.json"

usage() {
  sed -n '2,18p' "$0" | sed 's/^# //; s/^#//'
}

case "${1:-}" in
  --help|-h) usage; exit 0 ;;
  "") ;;
  *) echo "Unknown flag: $1" >&2; usage; exit 2 ;;
esac

mkdir -p "$OUT_DIR"

if [[ ! -d "$JUNIT_DIR" ]] || ! compgen -G "$JUNIT_DIR/*.xml" > /dev/null; then
  echo "WARN: no JUnit XML in $JUNIT_DIR — writing empty last-run.json"
  cat > "$OUT_FILE" <<EOF
{
  "ran_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "total": 0, "passed": 0, "failed": 0, "skipped": 0,
  "junit_dir": "$JUNIT_DIR",
  "cucumber_json": null,
  "failures": []
}
EOF
  echo ">>> wrote $OUT_FILE (no test runs found)"
  exit 0
fi

# Python is part of the toolchain on macOS/Linux dev boxes — use it for XML parsing.
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" > /dev/null 2>&1; then
  echo "ERROR: python3 not found; install or set PYTHON_BIN" >&2
  exit 3
fi

"$PYTHON_BIN" - <<PYEOF
import glob, json, os, re, sys, datetime
import xml.etree.ElementTree as ET

junit_dir = r"""$JUNIT_DIR"""
cucumber_json_path = r"""$CUCUMBER_JSON"""
out_file = r"""$OUT_FILE"""

total = passed = failed = skipped = 0
failures = []

# Index Cucumber JSON by scenario name + feature uri (for tags + line)
cuke_index = {}
if os.path.exists(cucumber_json_path):
    try:
        with open(cucumber_json_path, "r", encoding="utf-8") as f:
            cuke_features = json.load(f)
        for feat in cuke_features:
            uri = feat.get("uri", "")
            for el in feat.get("elements", []):
                name = el.get("name", "")
                line = el.get("line", 0)
                tags = [t.get("name") for t in el.get("tags", [])]
                key = (uri, name)
                cuke_index[key] = {"uri": uri, "line": line, "tags": tags}
    except Exception as e:
        print(f"WARN: cucumber.json parse failed: {e}", file=sys.stderr)

def stack_head(text, n=15):
    if not text:
        return ""
    lines = text.strip().splitlines()
    return "\n".join(lines[:n])

for xml_path in sorted(glob.glob(os.path.join(junit_dir, "*.xml"))):
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        print(f"WARN: parse failed {xml_path}: {e}", file=sys.stderr)
        continue
    root = tree.getroot()
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    for suite in suites:
        suite_tests = int(suite.attrib.get("tests", "0") or 0)
        suite_failures = int(suite.attrib.get("failures", "0") or 0)
        suite_errors = int(suite.attrib.get("errors", "0") or 0)
        suite_skipped = int(suite.attrib.get("skipped", "0") or 0)

        total += suite_tests
        failed += suite_failures + suite_errors
        skipped += suite_skipped

    for tc in root.findall(".//testcase"):
        name = tc.attrib.get("name", "")
        classname = tc.attrib.get("classname", "")
        f_el = tc.find("failure")
        e_el = tc.find("error")
        s_el = tc.find("skipped")
        if f_el is None and e_el is None:
            continue
        err = f_el if f_el is not None else e_el
        msg = err.attrib.get("message", "") if err is not None else ""
        typ = err.attrib.get("type", "") if err is not None else ""
        text = err.text or ""

        # Best-effort match to cucumber tags. Cucumber JUnit names look like:
        # "Scenario name" with classname = "Feature name".
        # We try (uri, scenario_name) match; if multiple, take first.
        scenario_meta = None
        for (uri, sname), meta in cuke_index.items():
            if sname == name:
                scenario_meta = meta
                break

        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "untitled"

        failures.append({
            "scenario": name,
            "classname": classname,
            "feature_uri": scenario_meta["uri"] if scenario_meta else None,
            "line": scenario_meta["line"] if scenario_meta else None,
            "tags": scenario_meta["tags"] if scenario_meta else [],
            "error_type": typ,
            "error_message": msg,
            "stack_head": stack_head(text),
            "slug": slug,
            "junit_xml": os.path.relpath(xml_path, start=os.getcwd()),
        })

passed = max(total - failed - skipped, 0)

out = {
    "ran_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "total": total,
    "passed": passed,
    "failed": failed,
    "skipped": skipped,
    "junit_dir": junit_dir,
    "cucumber_json": cucumber_json_path if os.path.exists(cucumber_json_path) else None,
    "failures": failures,
}

with open(out_file, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)

print(f">>> wrote {out_file}")
print(f"    total={total} passed={passed} failed={failed} skipped={skipped}")
if failures:
    print(f"    failed scenarios:")
    for fail in failures:
        tag_str = " ".join(fail["tags"]) if fail["tags"] else "(no tags)"
        loc = f"{fail['feature_uri']}:{fail['line']}" if fail['feature_uri'] else "(no feature loc)"
        print(f"      - {fail['scenario']}  [{tag_str}]  {loc}")
PYEOF
