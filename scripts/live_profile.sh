#!/usr/bin/env bash
# Rebuilds $JSA_PROFILE_DIR as a copy of profile.example/ at the live-check
# settings (docs/agentic_coding_team.md, CI environment): the cheapest settings
# that still exercise each integration. Run it again after changing
# profile.example/.
set -euo pipefail

target="${JSA_PROFILE_DIR:?JSA_PROFILE_DIR must be set}"
if [[ ! -d profile.example ]]; then
  echo "profile.example/ does not exist yet; nothing to copy."
  exit 0
fi

rm -rf "$target"
cp -r profile.example "$target"

# set_key <file> <section> <key> <value>: rewrites one key of one TOML table.
# A file or key that is not built yet is left alone.
set_key() {
  local file="$target/$1"
  [[ -f "$file" ]] || return 0
  awk -v section="[$2]" -v key="$3" -v value="$4" '
    /^\[/ { in_section = ($0 == section) }
    in_section && $0 ~ "^" key "[ \t]*=" { print key " = \"" value "\""; next }
    { print }' "$file" >"$file.tmp"
  mv "$file.tmp" "$file"
}

set_key search/search.toml runners.claude model claude-sonnet-5-5
set_key search/search.toml runners.claude effort low
set_key search/search.toml runners.gemini agent deep-research-preview-04-2026
for agent in checklist refine; do
  set_key config.toml "agents.$agent" model claude-sonnet-5-5
  set_key config.toml "agents.$agent" effort low
done
echo "Live-check profile written to $target"
