#!/usr/bin/env bash
# The tick's last step. Runs even when an earlier job failed.
#   TICK_OK  true when every job of this tick succeeded
#   WORKED   true when at least one agent job did work
source "$(dirname "$0")/lib.sh"

REPORT_EVERY=$(cfg '.report.every_merged_prs')

dispatch() {
  tick_gh workflow run "$1" -R "$REPO" --ref main
  echo "Dispatched $1"
}

# Completion: send the final report, then stop ticking until the owner
# re-enables the workflow.
complete=$(team_done)
if [[ "$TICK_OK" == true && "$complete" == true ]]; then
  dispatch "$REPORT_WORKFLOW"
  tick_gh workflow disable "$TICK_WORKFLOW" -R "$REPO"
  echo "The team is done; the tick is disabled."
  exit 0
fi

# A progress report every few merged feature PRs. A report already on its way
# has not moved the last-report time yet, so it is not dispatched twice.
since=$(last_report_time)
merged=$(feature_prs merged | jq --arg since "$since" '[.[] | select(.mergedAt > $since)] | length')
in_flight=$(gh run list -R "$REPO" --workflow "$REPORT_WORKFLOW" --event workflow_dispatch --limit 5 \
  --json status | jq '[.[] | select(.status != "completed")] | length')
if ((merged >= REPORT_EVERY && in_flight == 0)); then
  dispatch "$REPORT_WORKFLOW"
fi

# Work flows back-to-back while ticks succeed; after a failure, the cron
# interval is the retry delay.
if [[ "$TICK_OK" == true && "$WORKED" == true ]]; then
  dispatch "$TICK_WORKFLOW"
fi
