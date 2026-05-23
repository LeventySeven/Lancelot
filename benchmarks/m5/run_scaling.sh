#!/usr/bin/env bash
# M5 T1.6 — Multiprocess SO_REUSEPORT scaling (Linux only — see caveat).
#
# CAVEAT (M4 V3.3 finding): on macOS/BSD, SO_REUSEPORT binds N sockets but
# the kernel only delivers to ONE — so multi-process scaling on macOS shows
# a no-distribution floor (≈ single-process). The real scaling curve is
# Linux-only. This script can be run on macOS to validate the recipe + see
# the floor, but the published curve must come from the Linux VPS.
#
# For each (framework, workers ∈ {1, 2, 4, 8}): spawn N processes each
# binding 127.0.0.1:$PORT via SO_REUSEPORT (Lancelot's rust core + uvicorn
# both support it natively; Robyn/Blazeio require N manual processes).
#
# Output: benchmarks/results/M5/scaling/<framework>.workers_<N>.txt
set -uo pipefail

DUR="${DUR:-15s}"
C="${C:-100}"
SHAPE="${SHAPE:-tiny}"  # tiny | mid | large
WORKER_COUNTS="${WORKER_COUNTS:-1 2 4 8}"
VARIANT_FILTER="${VARIANT_FILTER:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

OUT="$RESULTS/scaling"
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
  esac
}

PLATFORM_OK=1
case "$(uname -s)" in
  Linux) ;;
  *) PLATFORM_OK=0
     echo "WARNING: $(uname -s) is NOT Linux — SO_REUSEPORT load-balancing"
     echo "         is LINUX-ONLY. macOS/BSD will bind N sockets but only"
     echo "         deliver to ONE — the scaling curve here is a"
     echo "         no-distribution floor. Run on Linux for real numbers."
     echo "" ;;
esac

echo "# M5 multiprocess SO_REUSEPORT scaling ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "# host=$(uname -sm) cpus=$N  dur=$DUR  c=$C  shape=$SHAPE"
echo "# worker counts: $WORKER_COUNTS"
[ "$PLATFORM_OK" = 0 ] && echo "# !! non-Linux — see caveat above"
echo ""

ek=$(shape_env_pair "$SHAPE" | awk '{print $1}')
ev=$(shape_env_pair "$SHAPE" | awk '{print $2}')
export "$ek=$ev"

# Spawn N processes of a variant on the SAME port via SO_REUSEPORT.
# Per the documented Lancelot deploy model (docs/deploy.md + M4 V3.3):
# the scaling unit is PROCESS, not thread. Each rust process runs with
# LANCELOT_WORKERS=1; the kernel SO_REUSEPORT distributes accepted
# conns across procs (Linux only — see top-of-file caveat).
#
# uvicorn is the special case: its --workers N master process forks N
# children that share the listening socket via fd inheritance, which
# DOES work on macOS (no SO_REUSEPORT kernel LB needed). We use that
# fork model for the uvicorn variants and N-independent-processes for
# the rest.
spawn_n() {
  local variant=$1 port=$2 workers=$3
  local pids=()
  case "$variant" in
    uvicorn-starlette|uvicorn-fastapi|uvicorn-lancelot)
      local factory
      case "$variant" in
        uvicorn-starlette) factory=starlette_factory ;;
        uvicorn-fastapi)   factory=fastapi_factory ;;
        uvicorn-lancelot)  factory=lancelot_factory ;;
      esac
      "$PY" -m uvicorn "servers:$factory" --factory --app-dir benchmarks \
        --host 127.0.0.1 --port "$port" --workers "$workers" \
        --loop uvloop --http httptools --log-level warning --no-access-log \
        >/dev/null 2>&1 &
      pids+=("$!")
      ;;
    rust)
      # N independent rust processes, each LANCELOT_WORKERS=1.
      for _ in $(seq 1 "$workers"); do
        LANCELOT_WORKERS=1 "$PY" benchmarks/servers.py rust "$port" >/dev/null 2>&1 &
        pids+=("$!")
      done
      ;;
    robyn|blazeio)
      # N independent processes — relies on Linux SO_REUSEPORT for LB.
      for _ in $(seq 1 "$workers"); do
        "$PY" benchmarks/servers.py "$variant" "$port" >/dev/null 2>&1 &
        pids+=("$!")
      done
      ;;
  esac
  echo "${pids[@]}"
}

stop_n() {
  for pid in "$@"; do
    kill -TERM "$pid" 2>/dev/null || true
    pkill -P "$pid" 2>/dev/null || true
  done
  sleep 0.5
  for pid in "$@"; do
    kill -9 "$pid" 2>/dev/null || true
  done
}

for v in "${M5_VARIANTS[@]}"; do
  want "$VARIANT_FILTER" "$v" || continue
  echo "## $v"
  for workers in $WORKER_COUNTS; do
    PORT=$((19000 + RANDOM % 500))
    # Need the worker-count to propagate via the appropriate env.
    case "$v" in
      rust) export LANCELOT_WORKERS=1 ;;  # 1 thread per proc; OS-level fork.
      *) ;;
    esac

    # For uvicorn variants, N=workers means uvicorn --workers N internally.
    # We achieve that by running the existing start_variant in fullmachine
    # mode but with N pinned to $workers via env.
    N_BACKUP="$N"
    N="$workers"
    pids=$(spawn_n "$v" "$PORT" "$workers")
    N="$N_BACKUP"

    # Wait for at least the first PID to bind the port (rest follow).
    first_pid=$(echo "$pids" | awk '{print $1}')
    if ! wait_listen "$PORT" "$first_pid" 30; then
      echo "  workers=$workers: FAILED to bind"
      stop_n $pids
      continue
    fi

    "$OHA" -z 2s -c "$C" --no-tui "http://127.0.0.1:$PORT/echo" >/dev/null 2>&1 || true
    out="$OUT/$v.workers_$workers.txt"
    "$OHA" -z "$DUR" -c "$C" --no-tui "http://127.0.0.1:$PORT/echo" >"$out" 2>&1 || true
    rps=$(grep -E 'Requests/sec' "$out" | tr -s ' \t' ' ' || true)
    succ=$(grep -E 'Success rate' "$out" | tr -s ' \t' ' ' || true)
    echo "  workers=$workers:$rps  $succ"

    stop_n $pids
    unset LANCELOT_WORKERS
  done
  echo ""
done

unset "$ek"
echo "# done — results in $OUT"
