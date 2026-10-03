#!/usr/bin/env bash
# Sends the report through Resend. Uses the reporter's draft when it carries
# every heading of the collected facts, in order; otherwise sends the facts, so
# a report never depends on the agent.
#   EXECUTION_FILE  the reporter run's output file, if the reporter ran
#
# The log is public: this prints no report text and no address.
source "$(dirname "$0")/lib.sh"

facts=report/facts.md
draft="${RUNNER_TEMP:?}/draft.md"
body="$facts"

headings() { grep -E '^## ' "$1" || true; }

if [[ -f "${EXECUTION_FILE:-}" ]]; then
  jq -rs 'flatten | [.[] | select(.type == "result")] | last | .result // empty' \
    "$EXECUTION_FILE" >"$draft" 2>/dev/null || true
fi
if [[ -s "$draft" && "$(headings "$draft")" == "$(headings "$facts")" ]]; then
  body="$draft"
  echo "Sending the reporter's draft."
else
  echo "Sending the collected facts: the reporter produced no draft with the required headings."
fi

payload=$(jq -n --arg from "$REPORT_FROM" --arg to "${REPORT_TO:?}" \
  --rawfile subject report/subject.txt --rawfile text "$body" \
  '{from: $from, to: [$to], subject: ($subject | rtrimstr("\n")), text: $text}')
code=$(curl -sS -o "$RUNNER_TEMP/resend.json" -w '%{http_code}' https://api.resend.com/emails \
  -H "Authorization: Bearer ${RESEND_API_KEY:?}" -H 'Content-Type: application/json' --data "$payload")
if [[ "$code" != 200 ]]; then
  echo "::error::Resend refused the email: HTTP $code $(jq -r '.name // ""' "$RUNNER_TEMP/resend.json" 2>/dev/null)"
  exit 1
fi
echo "Sent."
