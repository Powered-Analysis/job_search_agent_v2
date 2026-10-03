#!/usr/bin/env bash
# Shared readers of GitHub state for the tick's deterministic scripts
# (docs/agentic_coding_team.md, Orchestration). Sourced, never run.
#
# Reads use GH_TOKEN, the workflow's read-only token. Writes go through
# tick_gh, which needs TICK_TOKEN.
set -euo pipefail
# A failed read inside $(...) must stop the script, never read as empty state.
shopt -s inherit_errexit

REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set}"
PM_LOGIN="nickybell"
FSE_LOGIN="RoBOT-DeNiro"
SA_LOGIN="LeBOT-James"
SDET_LOGIN="Sandro-BOTicelli"
# Only these accounts' issues, comments, and reviews count as team state.
TEAM_JSON="[\"$PM_LOGIN\",\"$FSE_LOGIN\",\"$SA_LOGIN\",\"$SDET_LOGIN\"]"
# Paths only the owner changes: the specification, the team's operating
# agreement, and the owner's settings.
# shellcheck disable=SC2034  # read by invariants.sh and test_gate.sh
OWNER_PATHS=(prds docs team.yml)
TICK_WORKFLOW="tick.yml"
REPORT_WORKFLOW="report.yml"
EPOCH="1970-01-01T00:00:00Z"

# jq helpers shared by every filter that reads issues.
JQ_DEFS='
def has_label($l): (.labels | index($l)) != null;
# The text after "Name:" on its own line of an issue body.
def field($name):
  [(.body // "") | match("(?m)^[*_ ]*" + $name + ":[*_ ]*([^\r\n]*)") | .captures[0].string]
  | (.[0] // "") | gsub("^\\s+|\\s+$"; "");
def depends: field("Depends on") | [scan("#([0-9]+)") | .[0] | tonumber];
def first_line: split("\n")[0] | rtrimstr("\r");
# The FSE queue, in the order the FSE works it (Full Stack Engineer, Queue order).
def fse_queue:
  map(select(.state == "OPEN")) as $open
  | ($open | map(.number)) as $open_numbers
  | ($open | map(select(
      (has_label("needs-human") or has_label("follow-up") or has_label("revise-test")
        or (has_label("discrepancy") and (has_label("priority-now") | not))) | not))) as $eligible
  | ($eligible | map(select(has_label("in-progress"))))
    + ($eligible | map(select(has_label("priority-now") and (has_label("in-progress") | not))))
    + ($eligible | map(select(.labels == []
        and (depends | all(. as $d | ($open_numbers | index($d)) == null)))))
  | map(.number);
'

# The owner's settings. cfg <jq-path> fails on a missing key, never reads it as empty.
CONFIG_JSON=$(yq -o=json '.' "$(dirname "${BASH_SOURCE[0]}")/../team.yml")
cfg() { jq -er "$1" <<<"$CONFIG_JSON"; }

tick_gh() { GH_TOKEN="${TICK_TOKEN:?TICK_TOKEN must be set}" gh "$@"; }

# Every page of a REST list endpoint, as one JSON array.
api_list() { gh api --paginate "$1" --jq '.[]' | jq -s '.'; }

issue_url() { echo "https://github.com/$REPO/issues/$1"; }

# Every issue a team account wrote, open or closed, lowest number first.
team_issues() {
  gh issue list -R "$REPO" --state all --limit 1000 \
    --json number,title,body,state,author,labels,createdAt,closedAt |
    jq --argjson team "$TEAM_JSON" '[.[]
      | select(.author.login as $a | $team | index($a))
      | .labels |= map(.name)] | sort_by(.number)'
}

# Feature PRs are the FSE's PRs from feat/<issue-number>-<slug> branches.
feature_prs() { # state: open | merged | all
  gh pr list -R "$REPO" --state "$1" --base main --limit 1000 \
    --json number,url,state,headRefName,headRefOid,author,createdAt,updatedAt,mergedAt,reviewRequests |
    jq --arg fse "$FSE_LOGIN" '[.[]
      | select(.author.login == $fse and (.headRefName | startswith("feat/")))
      | {number, url, state, head: .headRefOid, createdAt, updatedAt, mergedAt,
         issue: ([.headRefName | capture("^feat/(?<n>[0-9]+)-") | .n | tonumber] | .[0]),
         requested: [.reviewRequests[] | .login // empty]}] | sort_by(.number)'
}

pr_reviews_raw() { api_list "repos/$REPO/pulls/$1/reviews?per_page=100"; }

# A PR's reviews that record a decision, oldest first. A stale approval that
# GitHub dismissed on a new push shows as DISMISSED.
pr_reviews() {
  pr_reviews_raw "$1" | jq '[.[]
    | select(.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED")
    | {login: .user.login, state, commit: .commit_id, at: .submitted_at}] | sort_by(.at)'
}

# Merged feature PRs with no SDET-Covers trailer on main naming them.
sdet_uncovered() {
  local covered
  # The API's own author filter misses a commit pushed moments ago, and the
  # discharge check runs right after the SDET's push, so the login is matched here.
  covered=$(gh api --paginate "repos/$REPO/commits?sha=main&per_page=100" \
    --jq ".[] | select(.author.login == \"$SDET_LOGIN\") | .commit.message" |
    { grep -E '^SDET-Covers:' || true; } | { grep -oE '#[0-9]+' || true; } | tr -d '#' |
    jq -Rs '[split("\n")[] | select(. != "") | tonumber]')
  feature_prs merged | jq --argjson covered "$covered" \
    '[.[] | .number | select(. as $n | ($covered | index($n)) == null)]'
}

# Each guard prints {"items": [...]} plus anything its job needs. An empty
# items list means the role has nothing to do this tick.

guard_pm() {
  local issues prs role
  issues=$(team_issues)
  prs=$(feature_prs open)
  role=$(cfg '.roles.pm')
  jq -n --argjson issues "$issues" --argjson prs "$prs" --argjson role "$role" --arg pm "$PM_LOGIN" "$JQ_DEFS"'
    ($prs | map(.issue)) as $linked
    | ((if ($issues | length) == 0 then ["plan"] else [] end)
      + [$prs[] | select(.requested | index($pm)) | "pr:\(.number)"]
      + [$issues[]
          | select(.state == "OPEN" and has_label("discrepancy"))
          | select((has_label("priority-now") or has_label("revise-test") or has_label("needs-human")) | not)
          | select(.number as $n | ($linked | index($n)) == null)
          | "discrepancy:\(.number)"]) as $items
    | {items: $items, effort: (if ($items | index("plan")) != null then $role.planning_effort else $role.effort end)}'
}

guard_fse() {
  local issues prs
  issues=$(team_issues)
  prs=$(feature_prs open)
  jq -n --argjson issues "$issues" --argjson prs "$prs" "$JQ_DEFS"'
    ($issues | fse_queue) as $queue
    | (if ($prs | length) == 0 then $queue[0:1]
       else $prs[0] as $pr
         | if ($pr.requested | length) == 0 and $pr.issue != null and ($queue | index($pr.issue)) != null
           then [$pr.issue] else [] end
       end) as $claim
    | {items: ($claim | map("issue:\(.)")), issue: ($claim[0] // "")}'
}

guard_sa() {
  feature_prs open | jq --arg sa "$SA_LOGIN" '
    map(select(.requested | index($sa))) as $prs
    | {items: ($prs | map("pr:\(.number)")), pr: ($prs[0].number // "")}'
}

guard_sdet() {
  local issues uncovered
  issues=$(team_issues)
  uncovered=$(sdet_uncovered)
  jq -n --argjson issues "$issues" --argjson uncovered "$uncovered" "$JQ_DEFS"'
    {items: (($uncovered | map("cover:\(.)"))
      + [$issues[] | select(.state == "OPEN" and has_label("revise-test")) | "revise:\(.number)"])}'
}

# Completion: planning has happened, only follow-up issues are open, no
# feature PR is open, and every merged feature PR has SDET coverage.
team_done() {
  local issues prs uncovered
  issues=$(team_issues)
  prs=$(feature_prs open)
  uncovered=$(sdet_uncovered)
  jq -n --argjson issues "$issues" --argjson prs "$prs" --argjson uncovered "$uncovered" "$JQ_DEFS"'
    ($issues | length) > 0
    and ($issues | all(.state != "OPEN" or has_label("follow-up")))
    and ($prs | length) == 0
    and ($uncovered | length) == 0'
}

# Completed ticks, newest first.
tick_runs() {
  gh run list -R "$REPO" --workflow "$TICK_WORKFLOW" --status completed --limit 30 \
    --json conclusion,startedAt |
    jq '[.[] | select(.conclusion != "cancelled" and .conclusion != "skipped")]'
}

# The start of report.yml's most recent successful dispatched run.
last_report_time() {
  gh run list -R "$REPO" --workflow "$REPORT_WORKFLOW" --event workflow_dispatch \
    --status success --limit 1 --json startedAt |
    jq -r --arg epoch "$EPOCH" '.[0].startedAt // $epoch'
}

# The computed Status (Progress reports, Sections): {"word": ..., "line": ...}.
report_status() {
  local issues merged runs complete limits
  limits=$(cfg '.report')
  issues=$(team_issues)
  merged=$(feature_prs merged)
  runs=$(tick_runs)
  complete=$(team_done)
  jq -n --argjson issues "$issues" --argjson merged "$merged" --argjson runs "$runs" \
    --argjson complete "$complete" --argjson limits "$limits" "$JQ_DEFS"'
    ($runs | map(.conclusion == "success")) as $ok
    | $limits.blocked_after_failed_ticks as $streak
    | (($runs | length) >= $streak and ($ok[0:$streak] | any | not)) as $blocked
    | ($ok | index(true)) as $first_ok
    | ((if $first_ok == null then $runs[-1] else $runs[$first_ok - 1] end).startedAt) as $blocked_since
    | ([$issues[] | select(.state == "OPEN" and has_label("needs-human"))] | length > 0) as $needs_input
    # The clock for a stall starts at the last merge, or at planning before any merge.
    | (($merged | map(.mergedAt) | max) // ($issues | map(.createdAt) | min)) as $last_progress
    | ($ok[0] == true and $last_progress != null
        and ($last_progress | fromdateiso8601) < (now - $limits.stalled_after_hours * 3600)) as $stalled
    | ([$issues[] | select(.state == "OPEN" and has_label("discrepancy"))] | length) as $discrepancies
    | (if $complete then {word: "Complete", line: "Complete"}
       elif $blocked then {word: "Blocked", line: "Blocked since \($blocked_since)"}
       elif $needs_input then {word: "Needs your input", line: "Needs your input"}
       elif $stalled then {word: "Stalled", line: "Stalled since \($last_progress)"}
       else {word: "On track", line: "On track"} end)
    | .line += (if $discrepancies > 0
        then ". Testing found \($discrepancies) place(s) where the build does not yet match the spec."
        else "" end)'
}
