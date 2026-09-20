#!/usr/bin/env bash
# ONE-OFF Phase 0 comparison spike driver -- NOT PRODUCTION CODE.
# See compare.mjs and ../jev_plugin_compare_export.py for what this measures.
#
# WARNING: makes REAL, BILLED Jev calls (<=6 via fast-jev-compaction, plus <=6
# more if TP is re-derived with jev_savings_spike.py).
#
#   ./run.sh              # T0/TH/TC only -- <=6 billed calls
#   ./run.sh --with-tp    # also re-derive TP -- <=12 billed calls
#   ./run.sh --dry-run    # NO billed calls: keep-everything asker, exercises
#                         # the whole adapt/compact/restore/recount path
#
# Every step is bounded in wall-clock time (see *_TIMEOUT below, seconds): the
# per-call caps in compare.mjs bound spend, not a hang, and compare.mjs's own
# per-request abort signal is backstopped here at the process level.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="${WORK:-$(mktemp -d -t jev-plugin-compare)}"
PY="$REPO/.venv/bin/python"
HELPER="$REPO/benchmarks/jev_plugin_compare_export.py"

EXPORT_TIMEOUT="${EXPORT_TIMEOUT:-900}"
COMPARE_TIMEOUT="${COMPARE_TIMEOUT:-1200}"
SPIKE_TIMEOUT="${SPIKE_TIMEOUT:-1200}"
REPORT_TIMEOUT="${REPORT_TIMEOUT:-300}"
REQUEST_TIMEOUT_MS="${REQUEST_TIMEOUT_MS:-120000}"

DRY_RUN=0
WITH_TP=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    --with-tp) WITH_TP=1 ;;
    *) echo "unknown flag: $a (want --dry-run and/or --with-tp)" >&2; exit 2 ;;
  esac
done

# Bound a command by wall clock. Prefers a real `timeout`/`gtimeout`; falls back
# to a watchdog subshell, because macOS has neither by default.
with_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout -k 10 "$secs" "$@"; return $?; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout -k 10 "$secs" "$@"; return $?; fi
  "$@" &
  local pid=$!
  ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null; sleep 10; kill -KILL "$pid" 2>/dev/null ) >/dev/null 2>&1 &
  local watchdog=$!
  local rc=0
  wait "$pid" || rc=$?
  kill -TERM "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  if [ "$rc" -ge 124 ]; then echo "TIMED OUT after ${secs}s: $*" >&2; fi
  return "$rc"
}

if [ ! -d "$HERE/vendor/fast-jev-compaction/dist" ]; then
  echo "vendored library not built -- running: npm --prefix '$HERE' run setup" >&2
  npm --prefix "$HERE" run setup
fi

# Reproducibility gate: the vendored checkout must still be the pinned revision,
# or the numbers below would not be comparable with an earlier run's.
node "$HERE/verify-vendor.mjs"

with_timeout "$EXPORT_TIMEOUT" "$PY" "$HELPER" export --out "$WORK/corpus.json" --limit 6

COMPARE_ARGS=(
  "$HERE/compare.mjs"
  --export "$WORK/corpus.json" --out "$WORK/tc.json"
  --helper "$HELPER" --python "$PY"
  --request-timeout-ms "$REQUEST_TIMEOUT_MS"
)
[ "$DRY_RUN" = 1 ] && COMPARE_ARGS+=(--dry-run)

# The library reads TYPESAFE_API_KEY out of the environment itself; the value is
# never printed here, and it is exported only inside this subshell (never onto a
# command line, where `ps` could read it).
(
  export TYPESAFE_API_KEY="${TYPESAFE_API_KEY:-${HEADROOM_JEV_API_KEY:-}}"
  with_timeout "$COMPARE_TIMEOUT" node "${COMPARE_ARGS[@]}"
)

SPIKE_ARG=()
if [ "$WITH_TP" = 1 ]; then
  if [ "$DRY_RUN" = 1 ]; then
    echo "--dry-run: skipping --with-tp (jev_savings_spike.py makes real billed calls)" >&2
  else
    with_timeout "$SPIKE_TIMEOUT" "$PY" "$REPO/benchmarks/jev_savings_spike.py" \
      --limit 6 --timeout-ms 30000 > "$WORK/spike.txt" 2>&1 || true
    SPIKE_ARG=(--spike-output "$WORK/spike.txt")
  fi
fi

with_timeout "$REPORT_TIMEOUT" "$PY" "$HELPER" report \
  --export "$WORK/corpus.json" --tc "$WORK/tc.json" "${SPIKE_ARG[@]}"
echo "artifacts in $WORK" >&2
