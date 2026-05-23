#!/usr/bin/env bash
# M5 T1.4 — Streaming TTFT (time-to-first-token) distributions across
# the M5 variant set, on 3 chunk-count shapes (64 / 256 / 1024).
#
# For each (variant, chunks) pair: start the variant, run the ttft_collector
# to open TOTAL_REQUESTS streams (concurrency capped at CONNECTIONS), record
# wall-clock TTFT per stream, output p50/p90/p95/p99.
#
# Output: benchmarks/results/M5/ttft/<chunks>/<variant>.json
set -uo pipefail

TOTAL_REQUESTS="${TOTAL_REQUESTS:-200}"
CONNECTIONS="${CONNECTIONS:-64}"
SHAPE_FILTER="${SHAPE_FILTER:-}"    # comma-list of chunk counts
VARIANT_FILTER="${VARIANT_FILTER:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

OUT="$RESULTS/ttft"
mkdir -p "$OUT"

want() {
  local filter=$1 val=$2
  [ -z "$filter" ] && return 0
  case ",$filter," in *",$val,"*) return 0 ;; esac
  return 1
}

echo "# M5 streaming TTFT ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "# host=$(uname -sm) cpus=$N"
echo "# total_requests=$TOTAL_REQUESTS  concurrent=$CONNECTIONS"
echo "# variants: ${M5_VARIANTS[*]}"
echo ""

PORT=18960
for chunks in 64 256 1024; do
  want "$SHAPE_FILTER" "$chunks" || continue
  mkdir -p "$OUT/$chunks"
  echo "## chunks=$chunks"
  export LC_BENCH_CHUNKS="$chunks"

  for v in "${M5_VARIANTS[@]}"; do
    want "$VARIANT_FILTER" "$v" || continue
    PORT=$((PORT + 1))
    SRV=$(start_variant "$v" "$PORT" percore)
    if ! wait_listen "$PORT" "$SRV" 60; then
      echo "  $v: FAILED to start"
      stop_variant "$SRV"
      continue
    fi

    # Warmup — one stream to JIT any lazy import paths.
    curl -s -m 5 -N "http://127.0.0.1:$PORT/stream" >/dev/null 2>&1 || true

    out_json="$OUT/$chunks/$v.json"
    summary=$("$PY" "$SCRIPT_DIR/ttft_collector.py" 127.0.0.1 "$PORT" \
        --path /stream \
        --connections "$CONNECTIONS" \
        --total-requests "$TOTAL_REQUESTS" \
        --json-out "$out_json" 2>&1 | head -2)
    echo "  $v: $summary"

    stop_variant "$SRV"
  done

  unset LC_BENCH_CHUNKS
  echo ""
done
echo "# done — results in $OUT"
