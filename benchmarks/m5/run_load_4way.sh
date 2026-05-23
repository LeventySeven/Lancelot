#!/usr/bin/env bash
# M5 T1.2 — Sustained load comparison across the M5 variant set
# (Lancelot rust + ASGI, FastAPI/Starlette on uvicorn, Robyn, Blazeio)
# on 4 payload shapes.
#
# Shapes:
#   tiny    — 23B JSON     (LC_BENCH_ITEMS=0)
#   mid     — ~5KB JSON    (LC_BENCH_ITEMS=100)
#   large   — ~70KB JSON   (LC_BENCH_ITEMS=1500)
#   stream  — 256 chunks   (LC_BENCH_CHUNKS=256)
#
# Duration defaults to 30m per (variant, shape) — the M5 sustained target.
# Override with DURATION=2m for fast local checks.
#
# Output: benchmarks/results/M5/load_4way/<shape>/<variant>.txt
set -uo pipefail

DURATION="${DURATION:-30m}"
CONNECTIONS="${CONNECTIONS:-50}"
WARMUP="${WARMUP:-3s}"
SHAPE_FILTER="${SHAPE_FILTER:-}"      # comma-list; empty = all
VARIANT_FILTER="${VARIANT_FILTER:-}"  # same

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

OUT="$RESULTS/load_4way"
mkdir -p "$OUT"

want() {
  local filter=$1 val=$2
  [ -z "$filter" ] && return 0
  case ",$filter," in *",$val,"*) return 0 ;; esac
  return 1
}

shape_env_pair() {
  case "$1" in
    tiny)   echo "LC_BENCH_ITEMS 0" ;;
    mid)    echo "LC_BENCH_ITEMS 100" ;;
    large)  echo "LC_BENCH_ITEMS 1500" ;;
    stream) echo "LC_BENCH_CHUNKS 256" ;;
  esac
}
shape_path() {
  case "$1" in
    stream) echo "/stream" ;;
    *)      echo "/echo" ;;
  esac
}

echo "# M5 4-way load ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "# oha=$($OHA --version) host=$(uname -sm) cpus=$N"
echo "# duration=$DURATION connections=$CONNECTIONS"
echo "# variants: ${M5_VARIANTS[*]}"
[ -n "$SHAPE_FILTER" ]   && echo "# shape filter: $SHAPE_FILTER"
[ -n "$VARIANT_FILTER" ] && echo "# variant filter: $VARIANT_FILTER"
echo ""

PORT=18900

for shape in tiny mid large stream; do
  want "$SHAPE_FILTER" "$shape" || continue
  mkdir -p "$OUT/$shape"
  ek=$(shape_env_pair "$shape" | awk '{print $1}')
  ev=$(shape_env_pair "$shape" | awk '{print $2}')
  path=$(shape_path "$shape")
  echo "### shape=$shape  ${ek}=${ev}  path=${path}"

  for v in "${M5_VARIANTS[@]}"; do
    want "$VARIANT_FILTER" "$v" || continue
    PORT=$((PORT + 1))
    # Export shape env to this variant's subprocess only.
    export "$ek=$ev"
    SRV=$(start_variant "$v" "$PORT" percore)
    if ! wait_listen "$PORT" "$SRV" 60; then
      echo "  $v: FAILED to start"
      stop_variant "$SRV"
      unset "$ek"
      continue
    fi

    url="http://127.0.0.1:$PORT$path"
    "$OHA" -z "$WARMUP" -c "$CONNECTIONS" --no-tui "$url" >/dev/null 2>&1 || true
    "$OHA" -z "$DURATION" -c "$CONNECTIONS" --no-tui "$url" \
      >"$OUT/$shape/$v.txt" 2>&1 || true
    r=$(grep -E 'Requests/sec' "$OUT/$shape/$v.txt" | tr -s ' \t' ' ' || true)
    echo "  $v:$r"

    stop_variant "$SRV"
    unset "$ek"
  done
  echo ""
done
echo "# done — results in $OUT"
