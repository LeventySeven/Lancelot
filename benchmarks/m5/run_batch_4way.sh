#!/usr/bin/env bash
# M5 T1.5 — @app.batch peer comparison.
#
# Same rate-limited mock upstream (semaphore cap=8, latency=50ms) wrapped in
# 4 framework-specific bench servers. Each exposes /solo (one upstream call
# per request); Lancelot additionally exposes /batch (coalesced via
# @app.batch). c=200 concurrent clients hammer /solo on each framework.
#
# The headline: at upstream cap=8, /solo throughput on every framework is
# bounded by the cap; Lancelot's /batch coalesces same-key requests so its
# throughput vastly exceeds the upstream cap.
#
# Output: benchmarks/results/M5/batch_4way/<framework>.<path>.txt
set -uo pipefail

DUR="${DUR:-8s}"
C="${C:-200}"
LC_UPSTREAM_MS="${LC_UPSTREAM_MS:-50}"
LC_UPSTREAM_CAP="${LC_UPSTREAM_CAP:-8}"
LC_BATCH_WINDOW_MS="${LC_BATCH_WINDOW_MS:-50}"
LC_MAX_BATCH="${LC_MAX_BATCH:-16}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

OUT="$RESULTS/batch_4way"
mkdir -p "$OUT"

export LC_UPSTREAM_MS LC_UPSTREAM_CAP LC_BATCH_WINDOW_MS LC_MAX_BATCH

echo "# M5 batch 4-way ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "# upstream cap=$LC_UPSTREAM_CAP latency=${LC_UPSTREAM_MS}ms | batch window=${LC_BATCH_WINDOW_MS}ms max=$LC_MAX_BATCH | c=$C dur=$DUR"
echo ""

BODY='{"k":"A","n":1}'
PORT=18970

# bash 3.2 (macOS default) lacks `declare -A`; use direct local vars per framework.
run_one() {
  local framework=$1 path=$2 port=$3
  local safe_path=${path//\//_}
  local out="$OUT/$framework.$safe_path.txt"

  # Reset stats before the run.
  curl -s -X POST -m 3 "http://127.0.0.1:$port/reset" >/dev/null 2>&1 || true

  "$OHA" -z "$DUR" -c "$C" -m POST -d "$BODY" \
    -H 'content-type: application/json' --no-tui \
    "http://127.0.0.1:$port$path" >"$out" 2>&1 || true

  local rps stats
  rps=$(grep 'Requests/sec' "$out" | tr -s ' \t' ' ' | cut -d' ' -f3 || echo "?")
  stats=$(curl -s -m 3 "http://127.0.0.1:$port/stats" 2>/dev/null || echo "{}")
  echo "  $path: $rps req/s"
  echo "        $stats"
}

for framework in lancelot fastapi robyn blazeio; do
  PORT=$((PORT + 1))
  echo "## $framework (port $PORT)"
  cd "$ROOT"
  "$PY" benchmarks/m5/batch_peer_bench.py "$framework" "$PORT" \
    >/tmp/m5_batch_$framework.log 2>&1 &
  SRV=$!

  if ! wait_listen "$PORT" "$SRV" 60; then
    echo "  FAILED to start (tail /tmp/m5_batch_$framework.log)"
    stop_variant "$SRV"
    continue
  fi

  # Warmup
  "$OHA" -z 1s -c 8 -m POST -d "$BODY" -H 'content-type: application/json' \
    --no-tui "http://127.0.0.1:$PORT/solo" >/dev/null 2>&1 || true

  run_one "$framework" /solo "$PORT"

  if [ "$framework" = "lancelot" ]; then
    run_one "$framework" /batch "$PORT"
  fi

  stop_variant "$SRV"
  echo ""
done

echo "# done — results in $OUT"
