#!/usr/bin/env bash
# Honest load comparison of Lancelot's Rust server vs uvicorn baselines.
# Same machine, loopback, same minimal GET /echo workload, oha load tool.
#
# Measures TWO fair shapes (see benchmarks/README.md for full methodology):
#   percore     — 1 worker each (Rust LANCELOT_WORKERS=1 vs single-process
#                 uvicorn). Isolates per-core transport+runtime efficiency.
#   fullmachine — N workers each (Rust default N-thread tokio vs uvicorn
#                 --workers N multi-process). Reflects real deployment shape.
# uvicorn runs its PRODUCTION stack (uvloop + httptools), not the h11 fallback.
# Two equal-duration runs per (shape,variant); both raw outputs are saved.
set -uo pipefail

PY="${PY:-.venv/bin/python}"
OHA="${OHA:-$HOME/.cargo/bin/oha}"
DURATION="${DURATION:-10s}"
CONNECTIONS="${CONNECTIONS:-50}"
N="${N:-$(sysctl -n hw.logicalcpu 2>/dev/null || nproc)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
RESULTS="benchmarks/results"
command -v "$OHA" >/dev/null 2>&1 || { echo "oha not found at $OHA"; exit 1; }

wait_listen() {  # port pid
  for _ in $(seq 1 200); do
    kill -0 "$2" 2>/dev/null || { echo "server pid gone"; return 1; }
    nc -z 127.0.0.1 "$1" 2>/dev/null && return 0
    sleep 0.1
  done
  echo "did not bind"; return 1
}

# start_server <shape> <variant> <port>  → echoes the master PID
start_server() {
  local shape=$1 variant=$2 port=$3
  if [ "$variant" = "rust" ]; then
    local w; [ "$shape" = "percore" ] && w=1 || w="$N"
    LANCELOT_WORKERS="$w" "$PY" benchmarks/servers.py rust "$port" >/dev/null 2>&1 &
    echo $!
  else
    local factory; case "$variant" in
      uvicorn-starlette) factory=starlette_factory ;;
      uvicorn-fastapi)   factory=fastapi_factory ;;
      uvicorn-lancelot)  factory=lancelot_factory ;;
    esac
    if [ "$shape" = "percore" ]; then
      "$PY" benchmarks/servers.py "$variant" "$port" >/dev/null 2>&1 &
      echo $!
    else
      "$PY" -m uvicorn "servers:$factory" --factory --app-dir benchmarks \
        --host 127.0.0.1 --port "$port" --workers "$N" \
        --loop uvloop --http httptools --log-level warning --no-access-log \
        >/dev/null 2>&1 &
      echo $!
    fi
  fi
}

echo "# Lancelot load comparison ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "# oha=$($OHA --version) host=$(uname -sm) cpus=$N"
echo "# duration=$DURATION connections=$CONNECTIONS  (uvicorn: uvloop+httptools)"
echo ""

PORT=8900
for shape in percore fullmachine; do
  mkdir -p "$RESULTS/$shape"
  echo "### shape=$shape"
  for v in rust uvicorn-starlette uvicorn-fastapi uvicorn-lancelot; do
    PORT=$((PORT + 1))
    SRV=$(start_server "$shape" "$v" "$PORT")
    if ! wait_listen "$PORT" "$SRV"; then echo "  $v: FAILED to start"; kill "$SRV" 2>/dev/null; pkill -P "$SRV" 2>/dev/null; continue; fi
    "$OHA" -z 2s -c "$CONNECTIONS" --no-tui "http://127.0.0.1:$PORT/echo" >/dev/null 2>&1 || true  # warmup
    for run in run1 run2; do
      "$OHA" -z "$DURATION" -c "$CONNECTIONS" --no-tui "http://127.0.0.1:$PORT/echo" \
        >"$RESULTS/$shape/$v.$run.txt" 2>&1 || true
    done
    r1=$(grep -E 'Requests/sec' "$RESULTS/$shape/$v.run1.txt" | tr -s ' \t' ' ')
    r2=$(grep -E 'Requests/sec' "$RESULTS/$shape/$v.run2.txt" | tr -s ' \t' ' ')
    echo "  $v:$r1 |$r2"
    kill -TERM "$SRV" 2>/dev/null; pkill -P "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null
  done
  echo ""
done
echo "# done"
