#!/usr/bin/env bash
# Usage: test_gate.sh <pr-number>
# Runs the test suite on a feature PR's base and head commits and decides the
# `test-gate` status (Software Architect, Test gate). The base is where the
# branch left main, so the comparison isolates the PR's own diff.
#
# This runs the PR's code, so it runs in a job that holds no secret.
source "$(dirname "$0")/lib.sh"

pr="${1:?pr number}"
work="${RUNNER_TEMP:?}/gate"
mkdir -p "$work"

info=$(feature_prs open | jq -c --argjson n "$pr" '.[] | select(.number == $n)')
head=$(jq -r '.head' <<<"$info")
issue=$(team_issues | jq -c --argjson n "$(jq '.issue' <<<"$info")" '[.[] | select(.number == $n)][0]')

git fetch -q origin main "+refs/pull/$pr/head:refs/remotes/pull/$pr"
base=$(git merge-base origin/main "$head")

# run_suite <name> <sha> <port>: writes the IDs of the tests that passed to
# $work/<name>.passed. Returns non-zero when the suite could not run at all.
run_suite() {
  local name="$1" sha="$2" port="$3" dir="$work/$1" log="$work/$1.log" rc=0
  : >"$work/$name.passed"
  git worktree add -q --detach "$dir" "$sha" || return 1
  [[ -f "$dir/pyproject.toml" ]] || return 0
  (cd "$dir" && env -u GH_TOKEN TURSO_DATABASE_URL="http://127.0.0.1:$port" \
    uv run --with pytest pytest -q -rA --tb=short -p no:cacheprovider \
    --continue-on-collection-errors) >"$log" 2>&1 || rc=$?
  echo "::group::$name suite at $sha (pytest exit $rc)"
  tail -n 80 "$log"
  echo "::endgroup::"
  # 0: all passed. 1: some failed. 5: no tests collected.
  [[ "$rc" == 0 || "$rc" == 1 || "$rc" == 5 ]] || return 1
  { grep -E '^PASSED ' "$log" || true; } | sed -E 's/^PASSED //' | sort -u >"$work/$name.passed"
}

run_suite base "$base" 8081 || {
  echo "::error::The test suite could not run on the base commit $base"
  exit 1
}
head_ran=true
run_suite head "$head" 8082 || head_ran=false

decide() {
  local regressions listed missing
  if [[ "$head_ran" == false ]]; then
    echo "failure|The test suite could not run on the head commit."
    return
  fi
  regressions=$(comm -23 "$work/base.passed" "$work/head.passed")
  if [[ -n "$regressions" ]]; then
    echo "$regressions" >"$work/regressions"
    echo "failure|$(wc -l <"$work/regressions" | tr -d ' ') test(s) that pass on the base fail on the head."
    return
  fi
  if jq -e "$JQ_DEFS"'has_label("discrepancy")' <<<"$issue" >/dev/null; then
    listed=$(jq -r '.body // ""' <<<"$issue" | { grep -oE 'tests/[^ `]+::[^ `,;)]+' || true; } | sort -u)
    if [[ -z "$listed" ]]; then
      echo "failure|The discrepancy issue lists no test IDs to check."
      return
    fi
    missing=$(comm -23 <(echo "$listed") "$work/head.passed")
    if [[ -n "$missing" ]]; then
      echo "$missing" >"$work/regressions"
      echo "failure|$(wc -l <"$work/regressions" | tr -d ' ') test(s) the discrepancy issue lists still fail on the head."
      return
    fi
  fi
  echo "success|No test that passes on the base fails on the head."
}

result=$(decide)
state="${result%%|*}"
description="${result#*|}"

echo "test-gate: $state - $description"
{
  echo "### Test gate: $state"
  echo "$description (base \`${base:0:7}\`, head \`${head:0:7}\`)"
  if [[ -f "$work/regressions" ]]; then
    echo
    sed 's/^/- `/; s/$/`/' "$work/regressions"
  fi
} >>"${GITHUB_STEP_SUMMARY:-/dev/null}"
{
  echo "state=$state"
  echo "description=$description"
  echo "sha=$head"
} >>"${GITHUB_OUTPUT:-/dev/null}"
