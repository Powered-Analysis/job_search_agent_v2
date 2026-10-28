#!/usr/bin/env bash
# Usage: suite.sh [<checkout-dir>] [<pytest option>...]
# Runs the full test suite (docs/agentic_coding_team.md, CI environment,
# Running the suite): split round-robin across one pytest process per CPU, each
# with its own fresh libSQL container, so the shards never share a database.
# Prints every shard's output, then a summary, and exits like one pytest run:
# 0 all passed, 1 some failed, 5 no tests collected, anything else could not run.
set -euo pipefail

scripts="$(cd "$(dirname "$0")" && pwd)"
dir="$(cd "${1:-.}" && pwd)"
shift $(($# > 0 ? 1 : 0))
IMAGE="ghcr.io/tursodatabase/libsql-server:latest"
shards="$(getconf _NPROCESSORS_ONLN)"
work="$(mktemp -d)"
containers=()
cleanup() {
  if ((${#containers[@]})); then docker rm -f "${containers[@]}" >/dev/null 2>&1 || true; fi
  rm -rf "$work"
}
trap cleanup EXIT

ports=()
for _ in $(seq "$shards"); do
  id=$(docker run -d -p 127.0.0.1::8080 "$IMAGE")
  containers+=("$id")
  ports+=("$(docker port "$id" 8080/tcp | head -n 1 | sed 's/.*://')")
done
bash "$scripts/wait_libsql.sh" "${ports[@]}"

# The suite's fixtures read TURSO_DATABASE_URL when they load, so collection
# needs one; GH_TOKEN never reaches the code under test.
pytest_in() { # <port> <pytest args...>
  local port="$1"
  shift
  (cd "$dir" && env -u GH_TOKEN TURSO_DATABASE_URL="http://127.0.0.1:$port" \
    uv run --with pytest pytest -p no:cacheprovider "$@")
}

collect_rc=0
pytest_in "${ports[0]}" --collect-only -q --continue-on-collection-errors \
  >"$work/collect.log" 2>&1 || collect_rc=$?
grep '::' "$work/collect.log" >"$work/ids" || true
if [[ ! -s "$work/ids" ]]; then
  cat "$work/collect.log"
  [[ "$collect_rc" == 5 ]] && exit 5
  exit 2
fi
if [[ "$collect_rc" != 0 ]]; then
  # A module that fails to import is missing from every shard; show why.
  echo "::group::collection errors (pytest exit $collect_rc)"
  cat "$work/collect.log"
  echo "::endgroup::"
fi

# Each shard gets its own --basetemp: concurrent runs sharing pytest's default
# temp root delete each other's directories.
pids=()
for ((k = 0; k < shards; k++)); do
  awk -v n="$shards" -v k="$k" '(NR - 1) % n == k' "$work/ids" >"$work/ids.$k"
  if [[ ! -s "$work/ids.$k" ]]; then
    echo 5 >"$work/rc.$k"
    : >"$work/log.$k"
    continue
  fi
  (
    rc=0
    pytest_in "${ports[$k]}" -q --basetemp="$work/tmp.$k" "$@" "@$work/ids.$k" \
      >"$work/log.$k" 2>&1 || rc=$?
    echo "$rc" >"$work/rc.$k"
  ) &
  pids+=("$!")
done
wait "${pids[@]}"

rc=0
all_empty=true
for ((k = 0; k < shards; k++)); do
  cat "$work/log.$k"
  shard_rc=$(cat "$work/rc.$k")
  [[ "$shard_rc" == 5 ]] || all_empty=false
  case "$shard_rc" in
    0 | 5) ;;
    1) ((rc == 0)) && rc=1 ;;
    *) rc="$shard_rc" ;;
  esac
done
[[ "$all_empty" == true ]] && rc=5
[[ "$collect_rc" != 0 && "$rc" == 0 ]] && rc=1

echo "==== suite summary: $(wc -l <"$work/ids" | tr -d ' ') tests in $shards shards ===="
cat "$work"/log.* | { grep -E '^(FAILED|ERROR) [^ ]+::' || true; } | sort -u
for ((k = 0; k < shards; k++)); do
  echo "shard $k (pytest exit $(cat "$work/rc.$k")): $(tail -n 1 "$work/log.$k")"
done
exit "$rc"
