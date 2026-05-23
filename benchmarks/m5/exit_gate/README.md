# M5 Exit-Gate Workload

The AI-shaped real workload deployed to Render (Phase B) and the VPS pair (Phase C) as the M5 exit gate. Same app code under each framework — only the framework binding differs.

## Routes (3 — proves the marquee wins together)

| Route | Method | Tests | Behavior |
|---|---|---|---|
| `/v1/chat/completions` | POST | transport + streaming + client-disconnect cancellation | Streaming passthrough to a mocked LLM upstream emitting 50 tok/s w/ 10ms TTFT |
| `/api/embed` | POST | large-payload serialization (above the 1-4 KB crossover) | Small JSON in → ~32 KB JSON out (embedding vector) |
| `/api/lookup` | POST | request coalescing | Calls a rate-limited mock backend (cap=8). **Lancelot uses `@app.batch`; peers route 1:1.** |
| `/healthz` | GET | const-tier (Lancelot) / plain | "ok" |

## Components

- `mock_upstream.py` — standalone async HTTP server that exposes:
  - `POST /v1/chat/completions` → emits SSE-shaped chunks at 50 tok/s with 10ms TTFT.
  - `POST /v1/lookup` → rate-limited (asyncio.Semaphore cap=8, 50ms/call).
  - The frameworks call this; treat it like a real LLM/vector backend.
- `app_lancelot.py` — Lancelot 3-route + `/healthz` on the const tier.
- `app_fastapi.py` — FastAPI equivalent. No `@app.batch` (1:1 lookup).
- `app_robyn.py` — Robyn equivalent.
- `app_blazeio.py` — Blazeio equivalent.

## Local smoke

```bash
# Terminal 1 — start the mock upstream
.venv/bin/python benchmarks/m5/exit_gate/mock_upstream.py 8901

# Terminal 2 — start one framework against it
LC_UPSTREAM_URL=http://127.0.0.1:8901 \
.venv/bin/python benchmarks/m5/exit_gate/app_lancelot.py 8910

# Terminal 3 — probe
curl -sN -X POST http://127.0.0.1:8910/v1/chat/completions -H 'content-type: application/json' -d '{"messages":[{"role":"user","content":"hi"}]}'
curl -s   -X POST http://127.0.0.1:8910/api/embed -H 'content-type: application/json' -d '{"text":"hello"}' | wc -c
curl -s   -X POST http://127.0.0.1:8910/api/lookup -H 'content-type: application/json' -d '{"k":"A","n":1}'
curl -s        http://127.0.0.1:8910/healthz
```

## Phase B — Render deploy

See `Dockerfile` + `render.yaml`. Each framework is its own Web Service:

```bash
# After connecting the repo in Render dashboard:
render services create --plan starter --name lancelot-exit-gate-lancelot ...
# or via the dashboard with the included render.yaml.
```

## Phase C — VPS deploy

See `deploy_vps.sh` (provisioning script for a fresh Ubuntu 22.04 VPS) + `nginx.conf` (TLS termination) + `systemd/*.service` (unit files for each framework).

```bash
ssh ubuntu@<vps-server-host> bash < deploy_vps.sh
```

## Env knobs

| Env | Default | Meaning |
|---|---|---|
| `LC_UPSTREAM_URL` | `http://127.0.0.1:8901` | The mock LLM/vector backend |
| `LC_EMBED_SIZE` | `4096` | Number of floats in the /api/embed response (4096 ≈ 32 KB JSON) |
| `LC_BATCH_WINDOW_MS` | `50` | Lancelot @app.batch window (Lancelot only) |
| `LC_BATCH_MAX` | `16` | Lancelot @app.batch max batch (Lancelot only) |
| `PORT` | `8910` | Server bind port (overridable by CLI arg, but env-first for Render) |
