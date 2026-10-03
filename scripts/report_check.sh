#!/usr/bin/env bash
# Decides whether this report.yml run sends an email, and of which kind.
# A dispatched run always sends a progress report. A cron run is the alert
# check (Orchestration, Alerts).
source "$(dirname "$0")/lib.sh"

ALERT_REPEAT_SECONDS=$((4 * 3600))

decide() {
  if [[ "$GITHUB_EVENT_NAME" == workflow_dispatch ]]; then
    echo "send=true"
    echo "kind=Progress"
    return
  fi
  echo "kind=Alert"

  # A disabled tick means the team is finished or has not been started.
  local tick_state word recent id
  tick_state=$(gh api "repos/$REPO/actions/workflows/$TICK_WORKFLOW" --jq '.state')
  if [[ "$tick_state" != active ]]; then
    echo "send=false"
    return
  fi

  word=$(report_status | jq -r '.word')
  case "$word" in
    Blocked | "Needs your input" | Stalled) ;;
    *)
      echo "send=false"
      return
      ;;
  esac

  # The last alert is the most recent cron run whose send job succeeded.
  recent=$(gh run list -R "$REPO" --workflow "$REPORT_WORKFLOW" --event schedule --limit 12 \
    --json databaseId,createdAt |
    jq -r --argjson window "$ALERT_REPEAT_SECONDS" \
      '.[] | select((.createdAt | fromdateiso8601) > (now - $window)) | .databaseId')
  for id in $recent; do
    [[ "$id" == "${GITHUB_RUN_ID:-}" ]] && continue
    if gh api "repos/$REPO/actions/runs/$id/jobs" \
      --jq '.jobs[] | select(.name == "send" and .conclusion == "success") | .id' | grep -q .; then
      echo "send=false"
      return
    fi
  done
  echo "send=true"
}

decision=$(decide)
echo "$decision" | tee -a "${GITHUB_OUTPUT:-/dev/null}"
