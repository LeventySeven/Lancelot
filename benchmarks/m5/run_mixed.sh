#!/usr/bin/env bash
# M5 T1.3 — Mixed adversarial+benign cohort.
#
# For each framework: spawn N benign clients via oha against /echo, AND
# spawn M slow-loris connections (dribbling 1 header byte every 200ms),
# in the SAME run. Assert benign p99 doesn't degrade vs the baseline.
#
# Lancelot must drop slow-loris at LANCELOT_HEADER_TIMEOUT_MS (default 15s)
# while keeping benign traffic flowing. Peers may not (fair result —
# strengthens the cloud-native pitch).
#
# Output: benchmarks/results/M5/mixed/<variant>.benign.txt  (oha dump)
#         benchmarks/results/M5/mixed/<variant>.slowloris.json
set -uo pipefail

DURATION="${DURATION:-30s}"
BENIGN_C="${BENIGN_C:-50}"
SLOWLORIS_C="${SLOWLORIS_C:-200}"
DRIBBLE_MS="${DRIBBLE_MS:-200}"
VARIANT_FILTER="${VARIANT_FILTER:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

OUT="$RESULTS/mixed"
mkdir -p "$OUT"

want() {
  local filter=$1 val=$2
  [ -z "$filter" ] && return 0
  case ",$filter," in *",$val,"*) return 0 ;; esac
  return 1
}

echo "# M5 mixed adversarial+benign cohort ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "# duration=$DURATION  benign_c=$BENIGN_C  slowloris_c=$SLOWLORIS_C  dribble_ms=$DRIBBLE_MS"
echo "# variants: ${M5_VARIANTS[*]}"
echo ""

PORT=18950
for v in "${M5_VARIANTS[@]}"; do
  want "$VARIANT_FILTER" "$v" || continue
  PORT=$((PORT + 1))
  echo "## $v (port $PORT)"
  SRV=$(start_variant "$v" "$PORT" percore)
  if ! wait_listen "$PORT" "$SRV" 60; then
    echo "  FAILED to start"
    stop_variant "$SRV"
    continue
  fi

  # Warmup
  "$OHA" -z 2s -c "$BENIGN_C" --no-tui "http://127.0.0.1:$PORT/echo" >/dev/null 2>&1 || true

  # Start slow-loris in the background.
  "$PY" "$SCRIPT_DIR/slow_loris.py" 127.0.0.1 "$PORT" \
      --connections "$SLOWLORIS_C" --dribble-ms "$DRIBBLE_MS" \
      --duration-s "${DURATION%s}" \
      --json-out "$OUT/$v.slowloris.json" \
      >"$OUT/$v.slowloris.stdout" 2>&1 &
  SL_PID=$!

  # Run benign oha for the same duration.
  "$OHA" -z "$DURATION" -c "$BENIGN_C" --no-tui "http://127.0.0.1:$PORT/echo" \
    >"$OUT/$v.benign.txt" 2>&1 || true

  # Wait for slow-loris to finish (it ends at its own duration).
  wait "$SL_PID" 2>/dev/null || true

  # Summarize.
  benign_rps=$(grep -E 'Requests/sec' "$OUT/$v.benign.txt" | tr -s ' \t' ' ' || true)
  benign_p99=$(grep -E '99\.00%' "$OUT/$v.benign.txt" | head -1 | tr -s ' ' || true)
  benign_succ=$(grep -E 'Success rate' "$OUT/$v.benign.txt" | tr -s ' \t' ' ' || true)
  sl_summary=$(tail -1 "$OUT/$v.slowloris.stdout" 2>/dev/null || echo "(slowloris log missing)")
  echo "  benign:$benign_rps  $benign_p99  $benign_succ"
  echo "  slowloris: $sl_summary"
  echo ""

  stop_variant "$SRV"
done
echo "# done — results in $OUT"
