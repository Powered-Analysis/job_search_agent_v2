#!/usr/bin/env bash
# Fails the tick when GitHub's state breaks an invariant (Orchestration,
# Invariants).
source "$(dirname "$0")/lib.sh"

failed=0
violation() {
  echo "::error::Invariant $1"
  failed=1
}

issues=$(team_issues)
prs=$(feature_prs open)

open=$(jq 'length' <<<"$prs")
((open <= 1)) || violation "1: $open feature PRs are open; at most one may be."

idle=$(jq -r --slurpfile issues <(json_file "$issues") --arg sa "$SA_LOGIN" --arg pm "$PM_LOGIN" "$JQ_DEFS"'
  $issues[0] as $issues
  | .[] | . as $pr
  | ([$issues[] | select(.number == $pr.issue)][0]) as $issue
  | select((($pr.requested | index($sa)) != null or ($pr.requested | index($pm)) != null
      or ($issue != null and ($issue | has_label("in-progress") or has_label("priority-now") or has_label("needs-human"))))
      | not)
  | .number' <<<"$prs")
for number in $idle; do
  violation "2: feature PR #$number is waiting on no one."
done

# The owner is the only author allowed on these paths. Judged on each path's
# latest commit, so an owner commit that restores it clears the check.
for path in "${OWNER_PATHS[@]}"; do
  author=$(gh api "repos/$REPO/commits?sha=main&path=$path&per_page=1" --jq '.[0] | .author.login // "unknown"')
  [[ "$author" == "$PM_LOGIN" ]] || violation "3: the latest commit on main touching $path is by $author, not the owner."
done

exit "$failed"
