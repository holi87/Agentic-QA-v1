#!/usr/bin/env bash
# QualityCat — Bug Report Helper
#
# Creates a per-bug file in `bugs/BUG-NNN-<slug>.md` with full schema skeleton
# (YAML frontmatter + body sections). Updates `bugs/README.md` index.
# Aligned with Testing Lab: AI Edition contest requirement: one file per bug.
#
# Usage:
#   ./scripts/new-bug.sh "<title>"           Create new bug file from title.
#   ./scripts/new-bug.sh --reindex            Rebuild bugs/README.md from bugs/BUG-*.md.
#   ./scripts/new-bug.sh --next-id            Print next BUG-NNN id, do nothing else.
#   ./scripts/new-bug.sh --help               Show this help.

set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
BUGS_DIR="$PROJECT_ROOT/bugs"
EVIDENCE_DIR="$PROJECT_ROOT/evidence"
INDEX_FILE="$BUGS_DIR/README.md"

usage() {
  sed -n '2,16p' "$0" | sed 's/^# //; s/^#//'
}

ensure_dirs() {
  mkdir -p "$BUGS_DIR" "$EVIDENCE_DIR"
  if [[ ! -f "$INDEX_FILE" ]]; then
    cat > "$INDEX_FILE" <<'EOF'
# Bug Index

Total: 0 (0 Critical, 0 High, 0 Medium, 0 Low, 0 Info)

| ID | Severity | Title | Component | OWASP | Status |
|---|---|---|---|---|---|

See `docs/standards/bug-reporting.md` for full schema.
EOF
  fi
}

next_id() {
  local last_num=0
  if [[ -d "$BUGS_DIR" ]]; then
    while IFS= read -r f; do
      local n
      n="$(basename "$f" | sed -nE 's/^BUG-([0-9]{3})-.*\.md$/\1/p')"
      [[ -z "$n" ]] && continue
      if (( 10#$n > last_num )); then
        last_num=$((10#$n))
      fi
    done < <(find "$BUGS_DIR" -maxdepth 1 -type f -name 'BUG-*.md' 2>/dev/null)
  fi
  printf "BUG-%03d" $((last_num + 1))
}

slugify() {
  local title="$1"
  echo "$title" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' \
    | cut -c1-50 \
    | sed -E 's/-+$//'
}

iso_now() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

read_frontmatter_field() {
  # $1 = file, $2 = field name
  awk -v key="$2" '
    BEGIN{ infm=0 }
    /^---[[:space:]]*$/ { infm = !infm; next }
    infm && $0 ~ "^"key"[[:space:]]*:" {
      sub("^"key"[[:space:]]*:[[:space:]]*","")
      gsub(/^"|"$/,"")
      print
      exit
    }
  ' "$1"
}

severity_rank() {
  case "$1" in
    Critical) echo 0 ;;
    High)     echo 1 ;;
    Medium)   echo 2 ;;
    Low)      echo 3 ;;
    Info)     echo 4 ;;
    *)        echo 9 ;;
  esac
}

reindex() {
  ensure_dirs
  local tmp
  tmp="$(mktemp)"

  local total=0 c_crit=0 c_high=0 c_med=0 c_low=0 c_info=0

  declare -a rows=()
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    local id sev title component owasp status rank
    id="$(read_frontmatter_field "$f" id)"
    sev="$(read_frontmatter_field "$f" severity)"
    title="$(read_frontmatter_field "$f" title)"
    component="$(read_frontmatter_field "$f" component)"
    owasp="$(read_frontmatter_field "$f" owasp)"
    status="$(read_frontmatter_field "$f" status)"
    [[ -z "$status" ]] && status="OPEN"
    rank="$(severity_rank "$sev")"
    rows+=("$rank|$id|$sev|$title|$component|$owasp|$status|$(basename "$f")")
    total=$((total + 1))
    case "$sev" in
      Critical) c_crit=$((c_crit+1)) ;;
      High)     c_high=$((c_high+1)) ;;
      Medium)   c_med=$((c_med+1)) ;;
      Low)      c_low=$((c_low+1)) ;;
      Info)     c_info=$((c_info+1)) ;;
    esac
  done < <(find "$BUGS_DIR" -maxdepth 1 -type f -name 'BUG-*.md' 2>/dev/null | sort)

  {
    echo "# Bug Index"
    echo
    echo "Total: $total ($c_crit Critical, $c_high High, $c_med Medium, $c_low Low, $c_info Info)"
    echo
    echo "| ID | Severity | Title | Component | OWASP | Status |"
    echo "|---|---|---|---|---|---|"
    if (( ${#rows[@]} > 0 )); then
      printf '%s\n' "${rows[@]}" \
        | sort -t'|' -k1,1n -k2,2 \
        | awk -F'|' '{ printf "| [%s](%s) | %s | %s | %s | %s | %s |\n", $2, $8, $3, $4, $5, $6, $7 }'
    fi
    echo
    echo "See \`docs/standards/bug-reporting.md\` for full schema."
  } > "$tmp"

  mv "$tmp" "$INDEX_FILE"
  echo ">>> Reindexed $INDEX_FILE — $total bug(s)"
}

create() {
  local title="$1"
  ensure_dirs
  local id slug filename
  id="$(next_id)"
  slug="$(slugify "$title")"
  if [[ -z "$slug" ]]; then
    slug="untitled"
  fi
  filename="$BUGS_DIR/${id}-${slug}.md"

  if [[ -e "$filename" ]]; then
    echo "ERROR: file exists: $filename" >&2
    exit 2
  fi

  cat > "$filename" <<EOF
---
id: $id
title: $title
severity: TBD
likelihood: TBD
component: TBD
owasp: TBD
iso25010: TBD
wcag: N/A
found_by: TBD
test: TBD
scenario: TBD
status: OPEN
opened_at: $(iso_now)
---

# $id: $title

## Steps to Reproduce

1. TBD

Repro command:
\`\`\`bash
TBD
\`\`\`

## Expected (per spec)

Spec source: TBD

\`\`\`
TBD
\`\`\`

## Actual

\`\`\`
TBD
\`\`\`

## Evidence

- evidence/$id/TBD

## Impact

TBD

## Suggested Fix

TBD
EOF

  mkdir -p "$EVIDENCE_DIR/$id"

  reindex

  echo ">>> Created $filename"
  echo ">>> Evidence dir: evidence/$id/"
}

main() {
  case "${1:-}" in
    ""|--help|-h)
      usage
      exit 0
      ;;
    --reindex)
      reindex
      ;;
    --next-id)
      next_id
      ;;
    -*)
      echo "Unknown flag: $1" >&2
      usage
      exit 2
      ;;
    *)
      create "$1"
      ;;
  esac
}

main "$@"
