#!/usr/bin/env bash
# @app.batch (C5 marquee) effectiveness benchmark. Drives the batched route and
# the per-request "solo" baseline against the SAME rate-limited simulated
# upstream (concurrency-capped, the realistic LLM constraint) and reports
# throughput + the coalescing ratio (requests per upstream call). See
# benchmarks/batch_bench.py for the server + the honest model.
set -uo pipefail

PY="${PY:-.venv/bin/python}"
OHA="${OHA:-$HOME/.cargo/bin/oha}"
DUR="${DUR:-8s}"; C="${C:-200}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
command -v "$OHA" >/dev/null 2>&1 || { echo "oha not found at $OHA"; exit 1; }

PORT=8813
LC_UPSTREAM_MS="${LC_UPSTREAM_MS:-50}" LC_UPSTREAM_CAP="${LC_UPSTREAM_CAP:-8}" \
LC_BATCH_WINDOW_MS="${LC_BATCH_WINDOW_MS:-50}" LC_MAX_BATCH="${LC_MAX_BATCH:-16}" \
  "$PY" benchmarks/batch_bench.py "$PORT" >/tmp/lc_batch_srv.log 2>&1 &
SRV=$!
for _ in $(seq 1 100); do nc -z 127.0.0.1 "$PORT" 2>/dev/null && break; kill -0 "$SRV" 2>/dev/null || break; sleep 0.1; done
if ! nc -z 127.0.0.1 "$PORT" 2>/dev/null; then echo "server failed: $(tail -5 /tmp/lc_batch_srv.log)"; kill "$SRV" 2>/dev/null; exit 1; fi

BODY='{"k":"A","n":1}'
echo "# @app.batch effectiveness ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
echo "# upstream cap=${LC_UPSTREAM_CAP:-8} latency=${LC_UPSTREAM_MS:-50}ms | window=${LC_BATCH_WINDOW_MS:-50}ms max_batch=${LC_MAX_BATCH:-16} | c=$C dur=$DUR"
echo ""
for path in /solo /batch; do
  curl -s -X POST "http://127.0.0.1:$PORT/reset" >/dev/null
  rps=$("$OHA" -z "$DUR" -c "$C" -m POST -d "$BODY" -H 'content-type: application/json' \
        --no-tui "http://127.0.0.1:$PORT$path" 2>&1 | grep 'Requests/sec' | tr -s ' \t' ' ' | cut -d' ' -f3)
  echo "## $path  →  $rps req/s"
  echo "   $(curl -s "http://127.0.0.1:$PORT/stats")"
done
kill -TERM "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null
echo "# done"
