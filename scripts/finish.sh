#!/usr/bin/env bash
# The tick's last step. Runs even when an earlier job failed.
#   TICK_OK  true when every job of this tick succeeded
#   WORKED   true when at least one agent job did work
source "$(dirname "$0")/lib.sh"

REPORT_EVERY=$(cfg '.report.every_merged_prs')
ALERT_REPEAT_SECONDS=$(($(cfg '.report.alert_repeat_hours') * 3600))
IDLE_WAIT_SECONDS=$(($(cfg '.orchestration.idle_wait_minutes') * 60))

# dispatch <workflow> [gh workflow run arguments]
dispatch() {
  tick_gh workflow run "$1" -R "$REPO" --ref main "${@:2}"
  echo "Dispatched $*"
}

# Every tick starts the next one. Work flows back-to-back while ticks succeed;
# an idle or failed tick waits first, which is the polling interval for the
# owner's actions and the retry delay after a failure.
next_tick() {
  if [[ "$TICK_OK" != true || "$WORKED" != true ]]; then
    sleep "$IDLE_WAIT_SECONDS"
  fi
  dispatch "$TICK_WORKFLOW"
}
# On exit, so a report step that fails below never stops the team.
trap next_tick EXIT

# Completion: send the final report, then stop ticking until the owner
# re-enables the workflow.
complete=$(team_done)
if [[ "$TICK_OK" == true && "$complete" == true ]]; then
  trap - EXIT
  dispatch "$REPORT_WORKFLOW"
  tick_gh workflow disable "$TICK_WORKFLOW" -R "$REPO"
  echo "The team is done; the tick is disabled."
  exit 0
fi

# A progress report every few merged feature PRs. A report already on its way
# has not moved the last-report time yet, so it is not dispatched twice.
since=$(last_report_time)
merged=$(feature_prs merged | jq --arg since "$since" '[.[] | select(.mergedAt > $since)] | length')
in_flight=$(report_runs report | jq '[.[] | select(.status != "completed")] | length')
if ((merged >= REPORT_EVERY && in_flight == 0)); then
  dispatch "$REPORT_WORKFLOW"
fi

# The alert check (Orchestration, Alerts). The last alert is the most recent
# alert run that succeeded or is still on its way.
word=$(report_status | jq -r '.word')
case "$word" in
  Blocked | "Needs your input" | Stalled)
    recent=$(report_runs alert | jq --argjson window "$ALERT_REPEAT_SECONDS" '[.[]
      | select(.status != "completed" or .conclusion == "success")
      | select((.startedAt | fromdateiso8601) > (now - $window))] | length')
    if ((recent == 0)); then
      dispatch "$REPORT_WORKFLOW" -f kind=Alert
    fi
    ;;
esac
