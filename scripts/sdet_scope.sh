#!/usr/bin/env bash
# Usage: sdet_scope.sh <main-sha-before-the-sdet-job>
# Fails the tick when the SDET job pushed a change outside tests/. Nothing else
# pushes to main while the SDET job runs, so the range is the SDET's commits.
set -euo pipefail

before="${1:?sha}"
git fetch -q origin main
outside=$(git diff --name-only "$before" origin/main | { grep -v '^tests/' || true; })
if [[ -n "$outside" ]]; then
  echo "::error::SDET commits touched files outside tests/: $(tr '\n' ' ' <<<"$outside")"
  exit 1
fi
