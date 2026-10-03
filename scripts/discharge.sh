#!/usr/bin/env bash
# Usage: discharge.sh <role> <items-json>
# Fails the tick when a role's guard still selects a work item its agent was
# just invoked for (Orchestration, Discharge check).
source "$(dirname "$0")/lib.sh"

role="${1:?role}"
invoked="${2:?items}"

stuck=$("guard_$role" | jq -r --argjson invoked "$invoked" '.items - (.items - $invoked) | .[]')
if [[ -n "$stuck" ]]; then
  echo "::error::The $role agent ended its run without discharging: $(tr '\n' ' ' <<<"$stuck")"
  exit 1
fi
