#!/usr/bin/env bash
# Rebuilds $JSA_PROFILE_DIR as a copy of profile.example/ at the live-check
# settings (docs/agentic_coding_team.md, CI environment): the cheapest settings
# that still exercise each integration. Run it again after changing
# profile.example/.
source "$(dirname "$0")/lib.sh"

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

model=$(cfg '.live_checks.claude_model')
effort=$(cfg '.live_checks.claude_effort')
set_key search/search.toml runners.claude model "$model"
set_key search/search.toml runners.claude effort "$effort"
set_key search/search.toml runners.gemini agent "$(cfg '.live_checks.gemini_agent')"
for agent in checklist refine; do
  set_key config.toml "agents.$agent" model "$model"
  set_key config.toml "agents.$agent" effort "$effort"
done
echo "Live-check profile written to $target"
