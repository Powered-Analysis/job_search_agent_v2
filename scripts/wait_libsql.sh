#!/usr/bin/env bash
# Usage: wait_libsql.sh <port>...
# Waits until each libSQL container answers its health check.
set -euo pipefail

for port in "$@"; do
  for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:$port/health" >/dev/null && continue 2
    sleep 1
  done
  echo "::error::libSQL on port $port never became healthy"
  exit 1
done
