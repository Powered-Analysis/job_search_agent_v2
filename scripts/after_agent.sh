#!/usr/bin/env bash
# Usage: after_agent.sh <role> <items-json>
# The checks that follow every agent job, in order: reconcile, the discharge
# check, the invariant check.
set -euo pipefail
scripts=$(dirname "$0")

bash "$scripts/reconcile.sh"
bash "$scripts/discharge.sh" "$1" "$2"
bash "$scripts/invariants.sh"
