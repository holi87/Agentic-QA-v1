#!/usr/bin/env bash
# QualityCat — Summary Builder
#
# Produces a jury-friendly reports/summary.md from:
#   - reports/last-run.json   (totals + failure metadata, written by extract-last-run.sh)
#   - bugs/BUG-*.md           (per-bug frontmatter — severity, status, component)
#
# Output (overwrites):
#   reports/summary.md
#
# Usage:
#   ./scripts/build-summary.sh
#   ./scripts/build-summary.sh --help

set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
REPORTS_DIR="$PROJECT_ROOT/reports"
BUGS_DIR="$PROJECT_ROOT/bugs"
LAST_RUN="$REPORTS_DIR/last-run.json"
OUT="$REPORTS_DIR/summary.md"

usage() {
  sed -n '2,14p' "$0" | sed 's/^# //; s/^#//'
}

case "${1:-}" in
  --help|-h) usage; exit 0 ;;
  "") ;;
  *) echo "Unknown flag: $1" >&2; usage; exit 2 ;;
esac

mkdir -p "$REPORTS_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" > /dev/null 2>&1; then
  echo "ERROR: python3 not found; install or set PYTHON_BIN" >&2
  exit 3
fi

"$PYTHON_BIN" - "$LAST_RUN" "$BUGS_DIR" "$OUT" "$PROJECT_ROOT" <<'PYEOF'
import json, os, re, sys, datetime
from collections import Counter, defaultdict

last_run_path, bugs_dir, out_path, project_root = sys.argv[1:5]

# --- Test run ----------------------------------------------------------------
run = None
if os.path.exists(last_run_path):
    try:
        with open(last_run_path, "r", encoding="utf-8") as f:
            run = json.load(f)
    except Exception as e:
        print(f"WARN: failed to parse {last_run_path}: {e}", file=sys.stderr)

ran_at = run.get("ran_at") if run else None
total  = run.get("total", 0) if run else 0
passed = run.get("passed", 0) if run else 0
failed = run.get("failed", 0) if run else 0
skipped = run.get("skipped", 0) if run else 0
failures = run.get("failures", []) if run else []

# Tag breakdown for failures
fail_tag_counts = Counter()
for fail in failures:
    for t in (fail.get("tags") or []):
        fail_tag_counts[t] += 1

# Known-bug vs unexpected
known_bug_fails = [f for f in failures if "@known-bug" in (f.get("tags") or [])]
unexpected_fails = [f for f in failures if "@known-bug" not in (f.get("tags") or [])]

# --- Bugs --------------------------------------------------------------------
def read_frontmatter(path):
    fm = {}
    inside = False
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.strip() == "---":
                    if not inside:
                        inside = True
                        continue
                    break
                if inside:
                    m = re.match(r"^([A-Za-z0-9_]+)\s*:\s*(.*)$", line)
                    if m:
                        fm[m.group(1)] = m.group(2).strip().strip('"')
    except Exception:
        pass
    return fm

bugs = []
if os.path.isdir(bugs_dir):
    for name in sorted(os.listdir(bugs_dir)):
        if not (name.startswith("BUG-") and name.endswith(".md")):
            continue
        path = os.path.join(bugs_dir, name)
        fm = read_frontmatter(path)
        fm["_file"] = name
        bugs.append(fm)

sev_order = ["Critical", "High", "Medium", "Low", "Info"]
sev_counts = Counter()
status_counts = Counter()
component_counts = Counter()
for b in bugs:
    sev_counts[b.get("severity", "TBD")] += 1
    status_counts[b.get("status", "OPEN")] += 1
    component_counts[b.get("component", "?")] += 1

# --- Render ------------------------------------------------------------------
generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def pct(n, d):
    return f"{(n / d * 100):.1f}%" if d else "—"

lines = []
lines.append("# Test Run Summary")
lines.append("")
lines.append(f"- Generated: `{generated_at}`")
if ran_at:
    lines.append(f"- Last run:  `{ran_at}`")
lines.append("")
lines.append("## Totals")
lines.append("")
lines.append("| Total | Passed | Failed | Skipped | Pass rate |")
lines.append("|------:|-------:|-------:|--------:|----------:|")
lines.append(f"| {total} | {passed} | {failed} | {skipped} | {pct(passed, total)} |")
lines.append("")

if unexpected_fails:
    lines.append(f"## ⚠️ Unexpected Failures ({len(unexpected_fails)})")
    lines.append("")
    lines.append("Failures without `@known-bug` — investigate via `/QC-claude-triage-run`.")
    lines.append("")
    lines.append("| Scenario | Tags | Location | Error |")
    lines.append("|---|---|---|---|")
    for f in unexpected_fails[:50]:
        tags = " ".join(f.get("tags") or []) or "—"
        uri = f.get("feature_uri") or ""
        line = f.get("line")
        loc = f"`{uri}:{line}`" if uri and line else "—"
        msg = (f.get("error_message") or "").replace("|", "\\|").splitlines()[0:1]
        msg = msg[0][:120] if msg else ""
        lines.append(f"| {f.get('scenario','?')} | {tags} | {loc} | {msg} |")
    lines.append("")

if known_bug_fails:
    lines.append(f"## Known-Bug Failures ({len(known_bug_fails)})")
    lines.append("")
    lines.append("Expected reds — tracked under `bugs/`.")
    lines.append("")
    for f in known_bug_fails:
        tags = [t for t in (f.get("tags") or []) if t.startswith("@bug-")]
        bug_tag = tags[0] if tags else "(no @bug-NNN tag)"
        lines.append(f"- `{bug_tag}` — {f.get('scenario','?')}")
    lines.append("")

if fail_tag_counts:
    lines.append("## Failures by Tag")
    lines.append("")
    lines.append("| Tag | Count |")
    lines.append("|---|---:|")
    for tag, n in fail_tag_counts.most_common():
        lines.append(f"| `{tag}` | {n} |")
    lines.append("")

lines.append("## Bugs")
lines.append("")
if not bugs:
    lines.append("_No bugs filed._")
else:
    lines.append(f"Total bugs: **{len(bugs)}**")
    lines.append("")
    lines.append("By severity:")
    lines.append("")
    lines.append("| " + " | ".join(sev_order) + " | Other |")
    lines.append("|" + "---:|" * (len(sev_order) + 1))
    other = sum(v for k, v in sev_counts.items() if k not in sev_order)
    row = [str(sev_counts.get(s, 0)) for s in sev_order] + [str(other)]
    lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    if status_counts:
        lines.append("By status: " + ", ".join(f"**{k}**={v}" for k, v in status_counts.items()))
        lines.append("")
    lines.append("Top components:")
    lines.append("")
    for comp, n in component_counts.most_common(8):
        lines.append(f"- `{comp}` — {n}")
    lines.append("")
    lines.append("Full index: [`bugs/README.md`](../bugs/README.md)")
    lines.append("")

lines.append("## Where to Look")
lines.append("")
lines.append("- Cucumber HTML — `cucumber/index.html`")
lines.append("- Allure static — `allure/index.html` (open in browser, no server)")
lines.append("- JUnit XML — `junit/*.xml`")
lines.append("- Raw run data — `last-run.json` (consumed by `/QC-claude-triage-run`)")
lines.append("- Playwright trace.zip — `evidence/playwright/<scenario>-trace.zip` (open at https://trace.playwright.dev)")
lines.append("")
lines.append("---")
lines.append("Generated by `scripts/build-summary.sh`.")

with open(out_path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")

print(f">>> wrote {out_path}")
print(f"    total={total} passed={passed} failed={failed} skipped={skipped} bugs={len(bugs)}")
PYEOF
