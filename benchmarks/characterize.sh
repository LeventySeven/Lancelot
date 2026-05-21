#!/usr/bin/env bash
# Characterization sweeps that ground the repositioned claims (serialization +
# streaming). Per-core, 1 worker each (Rust LANCELOT_WORKERS=1 vs single-process
# uvicorn on uvloop+httptools), loopback, oha. Honest peer comparison: Rust core
# vs minimal Starlette, each using its DEFAULT serializer/streaming path.
#
#   1. crossover  — /echo at growing payload sizes → find where Rust's native
#                   pydantic-core serialization overtakes Python json.dumps.
#   2. streaming  — /stream yielding many small chunks (AI token-stream shape)
#                   → R3 pump vs Starlette StreamingResponse.
set -uo pipefail

PY="${PY:-.venv/bin/python}"
OHA="${OHA:-$HOME/.cargo/bin/oha}"
DUR="${DUR:-8s}"
C="${C:-50}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
RES="benchmarks/results"
command -v "$OHA" >/dev/null 2>&1 || { echo "oha not found at $OHA"; exit 1; }

wl() { for _ in $(seq 1 150); do kill -0 "$2" 2>/dev/null || return 1; nc -z 127.0.0.1 "$1" 2>/dev/null && return 0; sleep 0.1; done; return 1; }
rps() { grep -E 'Requests/sec' "$1" | tr -s ' \t' ' ' | cut -d' ' -f3; }
p50() { grep -E '50.00%' "$1" | tr -s ' ' | sed 's/.*in //'; }

# bench <env-prefix> <variant> <path> <outfile>
bench() {
  local envp="$1" variant="$2" path="$3" out="$4" port=$((9100 + RANDOM % 300)) pid
  if [ "$variant" = rust ]; then
    env $envp LANCELOT_WORKERS=1 "$PY" benchmarks/servers.py rust "$port" >/dev/null 2>&1 & pid=$!
  else
    env $envp "$PY" benchmarks/servers.py "$variant" "$port" >/dev/null 2>&1 & pid=$!
  fi
  if ! wl "$port" "$pid"; then echo "  (failed to start $variant)"; kill "$pid" 2>/dev/null; return 1; fi
  local bytes; bytes=$(curl -s "http://127.0.0.1:$port$path" | wc -c | tr -d ' ')
  "$OHA" -z 2s -c "$C" --no-tui "http://127.0.0.1:$port$path" >/dev/null 2>&1 || true
  "$OHA" -z "$DUR" -c "$C" --no-tui "http://127.0.0.1:$port$path" >"$out" 2>&1 || true
  kill -TERM "$pid" 2>/dev/null; pkill -P "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  echo "$bytes"
}

echo "# characterization ($(date -u +%Y-%m-%dT%H:%M:%SZ)) host=$(uname -sm)"
echo "# per-core (1 worker each), c=$C, dur=$DUR, oha=$($OHA --version)"
echo ""

mkdir -p "$RES/crossover"
echo "## crossover sweep — /echo, growing payload (rust to_json vs starlette json.dumps)"
printf "%-8s %-10s %-14s %-14s %-10s\n" "items" "rustB" "rust_rps" "star_rps" "winner"
for items in 5 25 100 350 1500; do
  rb=$(bench "LC_BENCH_ITEMS=$items" rust /echo "$RES/crossover/rust.$items.txt")
  sb=$(bench "LC_BENCH_ITEMS=$items" uvicorn-starlette /echo "$RES/crossover/starlette.$items.txt")
  rr=$(rps "$RES/crossover/rust.$items.txt"); sr=$(rps "$RES/crossover/starlette.$items.txt")
  win=$(awk -v a="$rr" -v b="$sr" 'BEGIN{print (a>b)?"rust":"starlette"}')
  printf "%-8s %-10s %-14s %-14s %-10s\n" "$items" "$rb" "$rr" "$sr" "$win"
done
echo ""

mkdir -p "$RES/stream"
echo "## streaming — /stream, N small chunks (R3 pump vs StreamingResponse)"
printf "%-8s %-14s %-14s %-14s %-10s\n" "chunks" "rust_rps" "rust_p50" "star_rps" "winner"
for ch in 64 256 1024; do
  bench "LC_BENCH_CHUNKS=$ch" rust /stream "$RES/stream/rust.$ch.txt" >/dev/null
  bench "LC_BENCH_CHUNKS=$ch" uvicorn-starlette /stream "$RES/stream/starlette.$ch.txt" >/dev/null
  rr=$(rps "$RES/stream/rust.$ch.txt"); sr=$(rps "$RES/stream/starlette.$ch.txt"); rp=$(p50 "$RES/stream/rust.$ch.txt")
  win=$(awk -v a="$rr" -v b="$sr" 'BEGIN{print (a>b)?"rust":"starlette"}')
  printf "%-8s %-14s %-14s %-14s %-10s\n" "$ch" "$rr" "$rp" "$sr" "$win"
done
echo ""
echo "# done"
