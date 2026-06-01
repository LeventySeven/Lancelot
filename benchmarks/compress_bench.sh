#!/usr/bin/env bash
# M18 R1 — the gzip KEEP/REVERT benchmark.
#
# Decides whether `lancelot.compress.GZipMiddleware` (libdeflate encode OFF the
# event loop via run_in_executor, GIL released in Rust) beats Starlette's
# GZipMiddleware (zlib holding the GIL on the loop thread) by enough to justify
# owning a forever Rust codec. The mechanism is a single-process *concurrency*
# effect, so loopback CAN measure it (it is NOT the bandwidth win — that needs a
# real network and isn't what this tests).
#
# Three arms, identical Starlette base + routes, differ ONLY in the gzip layer:
#   A compress-none       no compression (throughput ceiling)
#   B compress-starlette  Starlette GZipMiddleware(compresslevel=9)
#   C compress-lancelot   lancelot.compress.GZipMiddleware(compresslevel=9, off-loop)
# Payload: GET /big = LC_BENCH_ITEMS≈1000 varied objects (~50 KB, ratio ~5.3x —
# LOW redundancy; a repeated string would compress in <0.2ms and hide CPU cost).
#
# Sections:
#   1. c=200 single-worker  — the primary KEEP/REVERT number (RPS + p99).
#   2. c=1   single-worker  — control: C≈B proves any c=200 win is STRUCTURAL
#                             concurrency, not per-call Rust speed.
#   3. co-tenant            — 50% /big gzip flood + a concurrent tiny /echo probe;
#                             the tiny route's p99 is the GIL-starvation signal.
#   4. multi-process (4 wk) — control: separate GILs → the gap should shrink/vanish.
# All raw oha dumps land under benchmarks/results/compress/.
set -uo pipefail

PY="${PY:-.venv/bin/python}"
OHA="${OHA:-$HOME/.cargo/bin/oha}"
ITEMS="${LC_BENCH_ITEMS:-1000}"
LEVEL="${LC_GZIP_LEVEL:-9}"
GZMIN="${LC_GZIP_MIN:-500}"
DURATION="${DURATION:-30s}"
CONN="${CONN:-200}"
WORKERS="${WORKERS:-4}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
RESULTS="benchmarks/results/compress"
mkdir -p "$RESULTS"
command -v "$OHA" >/dev/null 2>&1 || { echo "oha not found at $OHA"; exit 1; }

wait_listen() {  # port pid
  for _ in $(seq 1 200); do
    kill -0 "$2" 2>/dev/null || { echo "  server pid $2 gone"; return 1; }
    nc -z 127.0.0.1 "$1" 2>/dev/null && return 0
    sleep 0.1
  done
  echo "  port $1 did not bind"; return 1
}

# start single-process arm (uvicorn --workers 1 via servers.py entrypoint)
start_1() {  # variant port
  LC_BENCH_ITEMS="$ITEMS" LC_GZIP_LEVEL="$LEVEL" LC_GZIP_MIN="$GZMIN" \
    "$PY" benchmarks/servers.py "$1" "$2" >/dev/null 2>&1 &
  echo $!
}

# start multi-process arm (uvicorn --factory --workers N)
start_N() {  # factory port nworkers
  LC_BENCH_ITEMS="$ITEMS" LC_GZIP_LEVEL="$LEVEL" LC_GZIP_MIN="$GZMIN" \
    "$PY" -m uvicorn "servers:$1" --factory --app-dir benchmarks \
    --host 127.0.0.1 --port "$2" --workers "$3" \
    --loop uvloop --http httptools --log-level warning --no-access-log \
    >/dev/null 2>&1 &
  echo $!
}

stop() { kill -TERM "$1" 2>/dev/null; pkill -P "$1" 2>/dev/null; wait "$1" 2>/dev/null; }

# extract "RPS=<n> p99=<s>s" from an oha dump (oha prints summary RPS + the 99%
# line under "Latency distribution").
summarize() {  # file label
  local f=$1 label=$2 rps p99
  rps=$(grep -E 'Requests/sec' "$f" | head -1 | awk '{print $2}')
  p99=$(grep -E '99\.00% in|99% in' "$f" | head -1 | awk '{print $(NF-1), $NF}')
  printf '  %-26s RPS=%-12s p99=%s\n' "$label" "${rps:-NA}" "${p99:-NA}"
}

# run oha on one URL, 2 runs, save raw, print both summaries
bench_url() {  # tag url conn label
  local tag=$1 url=$2 c=$3 label=$4
  "$OHA" -z 3s -c "$c" -H 'Accept-Encoding: gzip' --no-tui "$url" >/dev/null 2>&1 || true  # warmup
  for run in run1 run2; do
    "$OHA" -z "$DURATION" -c "$c" -H 'Accept-Encoding: gzip' --no-tui "$url" \
      >"$RESULTS/$tag.$run.txt" 2>&1 || true
  done
  summarize "$RESULTS/$tag.run1.txt" "$label run1"
  summarize "$RESULTS/$tag.run2.txt" "$label run2"
}

echo "# M18 R1 compress bench ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "# oha=$($OHA --version) host=$(uname -sm)"
echo "# items=$ITEMS (~$(LC_BENCH_ITEMS=$ITEMS $PY -c 'import json,os;n=int(os.environ["LC_BENCH_ITEMS"]);p={"items":[{"id":i,"name":"item-%d"%i,"val":i*3.14} for i in range(n)]};print(len(json.dumps(p)))') B) gzip-level=$LEVEL minimum_size=$GZMIN duration=$DURATION"
echo ""

PORT=8960

# ===== Section 1: c=200 single-worker (PRIMARY) =====
echo "### 1. c=$CONN single-worker /big  (PRIMARY KEEP/REVERT)"
for arm in "A compress-none" "B compress-starlette" "C compress-lancelot"; do
  set -- $arm; tag=$1 variant=$2
  PORT=$((PORT+1)); SRV=$(start_1 "$variant" "$PORT")
  if ! wait_listen "$PORT" "$SRV"; then echo "  $tag $variant: FAILED"; stop "$SRV"; continue; fi
  bench_url "c200_$tag" "http://127.0.0.1:$PORT/big" "$CONN" "$tag $variant"
  stop "$SRV"
done
echo ""

# ===== Section 2: c=1 single-worker (control: per-call parity) =====
echo "### 2. c=1 single-worker /big  (control: C≈B ⇒ win is structural, not per-call)"
for arm in "A compress-none" "B compress-starlette" "C compress-lancelot"; do
  set -- $arm; tag=$1 variant=$2
  PORT=$((PORT+1)); SRV=$(start_1 "$variant" "$PORT")
  if ! wait_listen "$PORT" "$SRV"; then echo "  $tag $variant: FAILED"; stop "$SRV"; continue; fi
  bench_url "c1_$tag" "http://127.0.0.1:$PORT/big" 1 "$tag $variant"
  stop "$SRV"
done
echo ""

# ===== Section 3: co-tenant starvation (decisive) =====
echo "### 3. co-tenant: tiny /echo p99 WHILE /big gzip flood (c=$CONN on /big)"
echo "#    (held-GIL B should starve the tiny route; off-loop C should not)"
for arm in "B compress-starlette" "C compress-lancelot"; do
  set -- $arm; tag=$1 variant=$2
  PORT=$((PORT+1)); SRV=$(start_1 "$variant" "$PORT")
  if ! wait_listen "$PORT" "$SRV"; then echo "  $tag $variant: FAILED"; stop "$SRV"; continue; fi
  # Background flood on /big (gzip, c=CONN). Then probe tiny /echo with c=20.
  "$OHA" -z "$DURATION" -c "$CONN" -H 'Accept-Encoding: gzip' --no-tui \
    "http://127.0.0.1:$PORT/big" >"$RESULTS/cotenant_${tag}_bigflood.txt" 2>&1 &
  FLOOD=$!
  sleep 2  # let the flood saturate the single GIL
  "$OHA" -z 20s -c 20 -H 'Accept-Encoding: gzip' --no-tui \
    "http://127.0.0.1:$PORT/echo" >"$RESULTS/cotenant_${tag}_echo.txt" 2>&1 || true
  kill "$FLOOD" 2>/dev/null; wait "$FLOOD" 2>/dev/null
  summarize "$RESULTS/cotenant_${tag}_echo.txt" "$tag tiny /echo (under flood)"
  stop "$SRV"
done
echo ""

# ===== Section 4: multi-process control =====
echo "### 4. c=$CONN multi-process (--workers $WORKERS) /big  (control: gap should shrink/vanish)"
for arm in "B compress_starlette_factory" "C compress_lancelot_factory"; do
  set -- $arm; tag=$1 factory=$2
  PORT=$((PORT+1)); SRV=$(start_N "$factory" "$PORT" "$WORKERS")
  if ! wait_listen "$PORT" "$SRV"; then echo "  $tag $factory: FAILED"; stop "$SRV"; continue; fi
  bench_url "mp_$tag" "http://127.0.0.1:$PORT/big" "$CONN" "$tag (workers=$WORKERS)"
  stop "$SRV"
done
echo ""
echo "# done — raw dumps in $RESULTS/"
