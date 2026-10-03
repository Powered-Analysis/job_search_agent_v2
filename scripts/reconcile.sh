#!/usr/bin/env bash
# Applies the labels and review requests that follow mechanically from review
# state on the open feature PR (Orchestration, Reconcile). Idempotent: every
# rule is derived from current state, so a re-run after a crash changes nothing
# that is already right.
source "$(dirname "$0")/lib.sh"

REVISION_CAP=$(cfg '.orchestration.revision_cap')

add_label() { tick_gh api -X POST "repos/$REPO/issues/$1/labels" -f "labels[]=$2" >/dev/null; }
remove_label() { tick_gh api -X DELETE "repos/$REPO/issues/$1/labels/$2" >/dev/null; }

issues=$(team_issues)
prs=$(feature_prs open)

while read -r pr <&3; do
  number=$(jq -r '.number' <<<"$pr")
  issue=$(jq -r '.issue' <<<"$pr")
  labels=$(jq -c --argjson n "$issue" '[.[] | select(.number == $n)][0].labels // []' <<<"$issues")
  has() { jq -e --arg l "$1" 'index($l) != null' <<<"$labels" >/dev/null; }

  state=$(pr_reviews "$number" | jq --argjson pr "$pr" --arg sa "$SA_LOGIN" --arg pm "$PM_LOGIN" '
    (map(select(.login == $sa)) | last) as $sa_latest
    | (map(select(.login == $sa and .state != "CHANGES_REQUESTED")) | last) as $sa_approval
    | (map(select(.login == $pm and .state != "DISMISSED")) | last) as $pm_latest
    | {approved: ($sa_latest != null and $sa_latest.state == "APPROVED"
          and $sa_latest.commit == $pr.head
          and ($pm_latest == null or $pm_latest.at < $sa_latest.at)),
       concern: ($pm_latest != null and $pm_latest.state == "CHANGES_REQUESTED"
          and ($sa_approval == null or $pm_latest.at > $sa_approval.at)),
       changes: [.[] | select(.state == "CHANGES_REQUESTED" and (.login == $sa or .login == $pm)) | .at]}')

  # Rule 1: the SA approved the head commit, so the PR goes to the PM.
  if [[ $(jq -r '.approved' <<<"$state") == true ]]; then
    for label in in-progress priority-now; do
      if has "$label"; then
        remove_label "$issue" "$label"
        echo "PR #$number: removed $label from #$issue (SA approved)"
      fi
    done
    if ! jq -e --arg pm "$PM_LOGIN" '.requested | index($pm) != null' <<<"$pr" >/dev/null; then
      tick_gh api -X POST "repos/$REPO/pulls/$number/requested_reviewers" -f "reviewers[]=$PM_LOGIN" >/dev/null
      echo "PR #$number: requested the PM's review"
    fi
  fi

  # Rule 2: the PM raised a concern, so the issue goes to the top of the FSE's queue.
  if [[ $(jq -r '.concern' <<<"$state") == true ]] && ! has priority-now; then
    add_label "$issue" priority-now
    echo "PR #$number: applied priority-now to #$issue (PM concern)"
  fi

  # Rule 3: a revision loop that does not converge goes to the owner.
  if (($(jq '.changes | length' <<<"$state") >= REVISION_CAP)) && ! has needs-human; then
    reset=$(api_list "repos/$REPO/issues/$issue/events?per_page=100" |
      jq -r '[.[] | select(.event == "unlabeled" and .label.name == "needs-human") | .created_at] | max // ""')
    rounds=$(jq --arg reset "$reset" --argjson pr "$pr" \
      '([$pr.createdAt, $reset] | max) as $since | [.changes[] | select(. > $since)] | length' <<<"$state")
    if ((rounds >= REVISION_CAP)); then
      # shellcheck disable=SC2016  # $owner, $name, $number are GraphQL variables
      threads=$(gh api graphql -F owner="${REPO%/*}" -F name="${REPO#*/}" -F number="$number" -f query='
        query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) { pullRequest(number: $number) {
            reviewThreads(first: 100) { nodes { isResolved comments(first: 1) { nodes { url } } } } } } }' \
        --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not) | "- " + .comments.nodes[0].url')
      body="Needs-human: $(jq -r '.url' <<<"$pr") has had $rounds rounds of requested changes without converging; decide how the open review threads should be resolved."
      [[ -n "$threads" ]] && body+=$'\n\n'"$threads"
      # Comment before labeling: a crash in between repeats the comment next
      # tick, never leaves the label without its explanation.
      tick_gh api -X POST "repos/$REPO/issues/$issue/comments" -f body="$body" >/dev/null
      add_label "$issue" needs-human
      echo "PR #$number: applied needs-human to #$issue (revision cap)"
    fi
  fi
done 3< <(jq -c '.[] | select(.issue != null)' <<<"$prs")
