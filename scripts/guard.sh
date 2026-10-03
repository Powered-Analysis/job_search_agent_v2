#!/usr/bin/env bash
# Usage: guard.sh <pm|fse|sa|sdet> [--claim]
# Decides from GitHub's current state whether a role has work this tick, and
# hands the job its work items. With --claim, the FSE guard also labels the
# issue it selected `in-progress`.
source "$(dirname "$0")/lib.sh"

role="${1:?role}"
result=$("guard_$role")
work=$(jq -r '.items | length > 0' <<<"$result")

if [[ "$role" == fse && "${2:-}" == --claim && "$work" == true ]]; then
  issue=$(jq -r '.issue' <<<"$result")
  tick_gh api -X POST "repos/$REPO/issues/$issue/labels" -f 'labels[]=in-progress' >/dev/null
fi

jq '.' <<<"$result"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "work=$work"
    jq -r 'to_entries[] | "\(.key)=\(.value | if type == "string" then . else tojson end)"' <<<"$result"
  } >>"$GITHUB_OUTPUT"
fi
