#!/usr/bin/env bash
# Usage: sdet_scope.sh <main-sha-before-the-sdet-job>
# Fails the tick when the SDET job pushed a change outside tests/. The owner may
# merge a PR while the SDET job runs, and a merged PR has passed main's merge
# gate, so only direct pushes are checked: the SDET is the one account that can
# push to main without a PR.
set -euo pipefail

before="${1:?sha}"
git fetch -q origin main
outside=$(
  for sha in $(git rev-list "$before"..origin/main); do
    merged=$(gh api "repos/${GITHUB_REPOSITORY:?}/commits/$sha/pulls" --jq 'map(select(.merged_at != null)) | length')
    if [[ "$merged" == 0 ]]; then git diff-tree --no-commit-id --name-only -r "$sha"; fi
  done | { grep -v '^tests/' || true; } | sort -u
)
if [[ -n "$outside" ]]; then
  echo "::error::SDET commits touched files outside tests/: $(tr '\n' ' ' <<<"$outside")"
  exit 1
fi
