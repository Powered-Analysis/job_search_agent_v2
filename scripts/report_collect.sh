#!/usr/bin/env bash
# Usage: report_collect.sh <Progress|Alert>
# Collects the facts for a report into report/ (Orchestration, Progress
# reports): facts.md under the email's headings, issues/<n>.md with the full
# text of every issue and ruling the facts mention, and subject.txt.
#
# The log of a public repository is public, so this prints nothing it collects.
# It never reads PR descriptions or comment text from PRs.
source "$(dirname "$0")/lib.sh"

kind="${1:?kind}"
out=report
rm -rf "$out"
mkdir -p "$out/issues"

since=$(last_report_time)
status=$(report_status)
issues=$(team_issues)
prs=$(feature_prs all)

comments=$(api_list "repos/$REPO/issues/comments?since=$since&per_page=100" |
  jq --arg since "$since" '[.[] | select(.created_at > $since)
    | {issue: (.issue_url | split("/") | last | tonumber), login: .user.login, body, at: .created_at}]')

# Each needs-human issue with its latest Needs-human: line.
needs="[]"
for number in $(jq -r "$JQ_DEFS"'.[] | select(.state == "OPEN" and has_label("needs-human")) | .number' <<<"$issues"); do
  line=$(api_list "repos/$REPO/issues/$number/comments?per_page=100" |
    jq --argjson team "$TEAM_JSON" "$JQ_DEFS"'[.[]
      | select(.user.login as $a | $team | index($a))
      | select(.body | startswith("Needs-human:")) | .body | first_line] | last // "Needs-human: (no explanation was recorded)"')
  needs=$(jq --argjson n "$number" --argjson line "$line" '. + [{issue: $n, line: $line}]' <<<"$needs")
done

# Feature PRs with an SA finding marked needs-change (security): since the last
# report. Only the fact of the finding is collected, never its text.
security_pattern='(?m)^\s*needs-change \(security\):'
flagged=$(api_list "repos/$REPO/pulls/comments?since=$since&per_page=100" |
  jq --arg since "$since" --arg sa "$SA_LOGIN" --arg pattern "$security_pattern" '[.[]
    | select(.created_at > $since and .user.login == $sa and (.body | test($pattern)))
    | .pull_request_url | split("/") | last | tonumber]')
for number in $(jq -r --arg since "$since" '.[] | select(.updatedAt > $since) | .number' <<<"$prs"); do
  found=$(pr_reviews_raw "$number" |
    jq --arg since "$since" --arg sa "$SA_LOGIN" --arg pattern "$security_pattern" \
      'any(.[]; .submitted_at > $since and .user.login == $sa and ((.body // "") | test($pattern)))')
  if [[ "$found" == true ]]; then
    flagged=$(jq --argjson n "$number" '. + [$n]' <<<"$flagged")
  fi
done

facts=$(jq -n --arg repo "$REPO" --arg since "$since" --arg pm "$PM_LOGIN" \
  --argjson status "$status" --argjson issues "$issues" --argjson prs "$prs" \
  --argjson comments "$comments" --argjson needs "$needs" --argjson flagged "$flagged" "$JQ_DEFS"'
  def issue($n): [$issues[] | select(.number == $n)][0];
  def outcome: field("Outcome") as $o | if $o == "" then .title else $o end;
  def link: "[#\(.number)](https://github.com/\($repo)/issues/\(.number))";
  def section($title; $lines; $empty): ["## " + $title, ""] + (if ($lines | length) == 0 then [$empty] else $lines end) + [""];
  def section($title; $lines): section($title; $lines; "Nothing since the last report.");

  [$comments[] | select(.login == $pm and (.body | startswith("Ruling:"))) | select(issue(.issue) != null)] as $rulings
  | [$issues[] | select(.state == "CLOSED" and .closedAt > $since and field("Outcome") != "")] as $delivered
  | [$issues | fse_queue | .[0:3][] | issue(.)] as $next
  | [$issues[] | select(field("Roadmap") != "")] as $planned
  | [$prs[] | select(.number as $n | ($flagged | index($n)) != null)] as $security
  | ([$needs[].issue] + [$security[].issue | select(. != null)] + [$delivered[].number] + [$rulings[].issue] + [$next[].number]
      | unique | map(issue(.)) | map(select(. != null))) as $mentioned
  | {
      mentioned: $mentioned,
      rulings: $rulings,
      text: (
        section("Status"; [$status.line])
        + (if ($needs | length) > 0 then section("Needs your input";
            [$needs[] | "- \(.line) Concerning: \(issue(.issue) | outcome) (\(issue(.issue) | link))"]) else [] end)
        + (if ($security | length) > 0 then section("Security";
            [$security[] | "- \((issue(.issue // -1) // {title: "A change in review.", body: ""}) | outcome) The architect raised a security finding on this work. Fix merged: \(if .state == "MERGED" then "yes" else "not yet" end). ([review](\(.url)))"]) else [] end)
        + section("Delivered";
            [$delivered | group_by(field("Roadmap"))[] | ("**" + (.[0] | field("Roadmap") | if . == "" then "Other" else . end) + "**"), (.[] | "- \(outcome) (\(link))")])
        + section("Decided";
            [$rulings[] | "- \(.body | first_line) Concerning: \(issue(.issue) | outcome) (\(issue(.issue) | link))"])
        + section("Roadmap";
            [$planned | group_by(field("Roadmap"))[] | "- \(.[0] | field("Roadmap")): \(map(select(.state == "CLOSED")) | length) of \(length) done"];
            "The work is not planned yet.")
        + section("Next up"; [$next[] | "- \(outcome) (\(link))"]; "Nothing is queued.")
        + section("Links"; [$mentioned[] | "- #\(.number) \(.title): https://github.com/\($repo)/issues/\(.number)"]; "None.")
        | join("\n"))
    }')

jq -r '.text' <<<"$facts" >"$out/facts.md"
for number in $(jq -r '.mentioned[].number' <<<"$facts"); do
  jq -r --argjson n "$number" '
    (.mentioned[] | select(.number == $n)) as $issue
    | "# #\($n) \($issue.title)\n\n\($issue.body // "")\n"
      + ([.rulings[] | select(.issue == $n) | "\n## Ruling (\(.at))\n\n\(.body)\n"] | join(""))' \
    <<<"$facts" >"$out/issues/$number.md"
done
echo "[Job Search Agent] $kind — $(jq -r '.word' <<<"$status")" >"$out/subject.txt"

# The reporter's model, effort, and turn cap, for its step.
cfg '.roles.reporter | to_entries[] | "\(.key)=\(.value)"' >>"${GITHUB_OUTPUT:-/dev/null}"

echo "Collected facts for a $kind email ($(jq '.mentioned | length' <<<"$facts") issues mentioned)."
