#!/usr/bin/env bash
# Shared helpers for M5 4-framework bench scripts (run_load_4way.sh,
# run_mixed.sh, run_ttft.sh, run_batch_4way.sh).
#
# All variants exposed by benchmarks/servers.py:
#   rust              — Lancelot's native pyo3+hyper server (R3 pump path)
#   uvicorn-starlette — Starlette on uvicorn[standard] (uvloop+httptools)
#   uvicorn-fastapi   — FastAPI on same
#   uvicorn-lancelot  — Lancelot's ASGI surface on same
#   robyn             — Robyn 0.84.0 (pyo3 + actix-web; own runtime)
#   blazeio           — Blazeio 4.1.0 (pure-Python asyncio + bespoke HTTP)

set -uo pipefail

PY="${PY:-.venv/bin/python}"
OHA="${OHA:-$HOME/.cargo/bin/oha}"
ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
RESULTS="${RESULTS:-$ROOT/benchmarks/results/M5}"
N="${N:-$(sysctl -n hw.logicalcpu 2>/dev/null || nproc)}"

command -v "$OHA" >/dev/null 2>&1 || { echo "oha not found at $OHA"; return 1 2>/dev/null || exit 1; }

# wait_listen <port> <pid> [<seconds>]  → 0 if bound, 1 otherwise
wait_listen() {
  local port=$1 pid=$2 max=${3:-30}
  local i=$((max * 10))
  for _ in $(seq 1 "$i"); do
    kill -0 "$pid" 2>/dev/null || { echo "server pid $pid gone (variant probably failed to import — check servers.py)"; return 1; }
    nc -z 127.0.0.1 "$port" 2>/dev/null && return 0
    sleep 0.1
  done
  echo "did not bind port=$port pid=$pid in ${max}s"; return 1
}

# start_variant <variant> <port> [<shape:percore|fullmachine>]
#   echoes the master PID. shape defaults to "percore" (1 worker each).
start_variant() {
  local variant=$1 port=$2 shape=${3:-percore}
  cd "$ROOT"
  case "$variant" in
    rust)
      local w
      if [ "$shape" = "fullmachine" ]; then w="$N"; else w=1; fi
      LANCELOT_WORKERS="$w" "$PY" benchmarks/servers.py rust "$port" >/dev/null 2>&1 &
      echo $!
      ;;
    uvicorn-starlette|uvicorn-fastapi|uvicorn-lancelot)
      local factory
      case "$variant" in
        uvicorn-starlette) factory=starlette_factory ;;
        uvicorn-fastapi)   factory=fastapi_factory ;;
        uvicorn-lancelot)  factory=lancelot_factory ;;
      esac
      if [ "$shape" = "fullmachine" ]; then
        "$PY" -m uvicorn "servers:$factory" --factory --app-dir benchmarks \
          --host 127.0.0.1 --port "$port" --workers "$N" \
          --loop uvloop --http httptools --log-level warning --no-access-log \
          >/dev/null 2>&1 &
        echo $!
      else
        "$PY" benchmarks/servers.py "$variant" "$port" >/dev/null 2>&1 &
        echo $!
      fi
      ;;
    robyn)
      # Robyn has its own runtime; not ASGI. Single-process; multiprocess
      # scaling is via OS-level worker forks not exposed by us.
      "$PY" benchmarks/servers.py robyn "$port" >/dev/null 2>&1 &
      echo $!
      ;;
    blazeio)
      # Blazeio single asyncio loop per process. Multiprocess scaling via
      # SO_REUSEPORT (Linux-only LB) — same model as Lancelot's docs/deploy.md.
      "$PY" benchmarks/servers.py blazeio "$port" >/dev/null 2>&1 &
      echo $!
      ;;
    *)
      echo "unknown variant: $variant" >&2
      return 1
      ;;
  esac
}

stop_variant() {
  local pid=$1
  kill -TERM "$pid" 2>/dev/null || true
  pkill -P "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

# Frameworks under test for M5. Robyn + Blazeio added this milestone.
M5_VARIANTS=(rust uvicorn-starlette uvicorn-fastapi uvicorn-lancelot robyn blazeio)

# Just the framework heads (one per project) for headline-comparison runs.
# rust = Lancelot's native fast path; uvicorn-fastapi = FastAPI baseline;
# robyn + blazeio = the two named peers.
M5_HEADS=(rust uvicorn-fastapi robyn blazeio)
