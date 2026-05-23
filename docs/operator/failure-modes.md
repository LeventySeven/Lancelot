# Lancelot Failure Modes — what happens at the edges

A single-page synthesis of every adversarial / overload / degraded-upstream condition Lancelot has been tested under (M4 validation + M5 soak), with the documented outcome, metric signature, and recovery action.

Every row in this table is backed by a tagged test or bench result. If you encounter a behavior that ISN'T in this table, file an issue — it might be an undocumented failure mode.

## Per-request adversarial

| Input | Outcome | Metric signature | Recovery action | Evidence |
|---|---|---|---|---|
| Client dribbles request headers byte-by-byte | Conn dropped at `LANCELOT_HEADER_TIMEOUT_MS` (default 15s); health requests unaffected | `requests` doesn't increase; connection terminations in kernel logs | None — automatic. For flood volumes block at nginx | M4 V1.1 ([`tests/test_m4_resilience_load.py`](../../tests/test_m4_resilience_load.py)) |
| Client sends > 16 MiB body | 413 Payload Too Large; RSS stays bounded | `requests{status="413"}` increment | Increase `App(max_body_bytes=...)` if legitimate; block at nginx if not | M4 V1.2 |
| Concurrent conns > `LANCELOT_MAX_CONNECTIONS` (default 1024) | Kernel-backlog backpressure; SIGTERM still responsive | `requests.in_flight` pinned at cap | Scale horizontally via `SO_REUSEPORT` | M4 V1.3 |
| Client disconnects mid-stream | In-flight upstream LLM call cancelled (TransportPool C2); user generator `finally` runs | `lancelot.http.requests` shows the truncated request as a 499-equivalent | None — automatic. Verify in `transport_pool` metrics | M4 V2.2 ([`tests/test_m4_ai_load.py`](../../tests/test_m4_ai_load.py)) |

## Per-request degraded path

| Input | Outcome | Metric signature | Recovery action | Evidence |
|---|---|---|---|---|
| Handler takes > per-route `timeout=` | 504 Gateway Timeout; yield-dep teardown runs; slot freed | `requests{status="504"}` increment | None — automatic. If happening a lot, the handler needs work | M4 V1.5 |
| Handler raises an exception | 500 with the registered exception handler (or default Starlette 500); request-id preserved in response | `requests{status="500"}` increment | Read the log line; the `request_id` correlates | M2 Tier-B |
| Upstream LLM is dead (TransportPool calls fail) | 502/503 bubbles to client; pool circuit-breaker state changes (if wired) | `requests{status="5xx"}` spike on routes that use the pool | Wait for upstream recovery; LB-level fallback if you have one | M2 Tier-C2 |
| `@app.batch` window fires with an awaiting handler | Handler runs to completion; results dispatched per-key (M3 batch bug fix) | `batch_calls` increments | None — automatic. M4 V2.1 validated multi-key fairness | M2 Tier-C5 + M3 batch validated |
| `@app.batch` queue full beyond `max_keys` | New key gets 503 with `batch full`; existing keys unaffected | `requests{status="503"}` | Increase `max_keys` if legitimate; rekey if one key dominates | M4 V2.1 |

## Deploy / lifecycle

| Input | Outcome | Metric signature | Recovery action | Evidence |
|---|---|---|---|---|
| `SIGTERM` mid-load | Graceful drain via `acquire_many_owned(max_conns)` bounded by `LANCELOT_DRAIN_SECS`. In-flight requests finish | `requests.in_flight` drops to 0 within drain window | None if drain succeeds; bump `LANCELOT_DRAIN_SECS` if not | M4 V1.4 |
| Drain timeout exceeded | Process exits with non-zero status; deploy tool should force SIGKILL only after drain timeout (NOT before) | Final log line shows residual in-flight count | Check why handlers can't finish in `LANCELOT_DRAIN_SECS`; bound their work explicitly | M4 V1.4 |
| Lancelot rust core multi-threaded (LANCELOT_WORKERS > 1) | Anti-scales — Python GIL serializes; throughput goes DOWN past 4 threads | Process CPU > 1 core's worth, but RPS < single-thread case | Deploy multiprocess (`LANCELOT_WORKERS=1` per process, N processes via `SO_REUSEPORT`) | Honest bench [`benchmarks/README.md`](../../benchmarks/README.md) + M3 bench characterization |
| `SO_REUSEPORT` on macOS | Binds N sockets, kernel delivers to ONE | Scaling curve flat (no LB across procs) | Linux only; deploy to Linux for real scaling | M4 V3.3 ([`docs/deploy.md`](../deploy.md)) |

## Sustained / endurance

| Input | Outcome | Metric signature | Recovery action | Evidence |
|---|---|---|---|---|
| Steady moderate load for hours | Latency p99 stable; RSS/FD/task counts flat | Grafana RED panel steady; no slope on memory | None — baseline. Monitor for slope | M5 T2.a soak |
| Slow leak in user code (e.g. unclosed `httpx.AsyncClient`) | RSS grows ~linearly; latency stays good until OOM | `soak.py --remote-host` RSS column grows monotonically | Find the leak — it's almost always in app code; M4 validated framework leak-free under burst+adversarial | M5 T2.a guard G3 |
| FD leak | FD count grows; eventually `EMFILE` errors | `soak.py` `fd_count` column trends up | `lsof -p <pid>` to identify the type leaking | M5 T2.a guard G4 |

## Behaviors NOT a failure mode (frequently mistaken)

| Symptom | This is | Not a bug |
|---|---|---|
| Small `/echo` slower than uvicorn+uvloop | The per-request FFI+asyncio-bridge overhead is real | We optimize for mid/large payloads + streaming + batch; lead with those |
| Streaming `@app.stream` per-chunk overhead higher than raw Starlette | Real LLM streaming is upstream-rate-limited (~50-200 tok/s); the per-chunk overhead is below the bottleneck | Not the right metric to optimize |
| `BaseHTTPMiddleware` ordering subtle difference | One documented divergence; affects 1 conformance test | See [`docs/migration/from-fastapi.md`](../migration/from-fastapi.md) §1 |
| `json.dumps` mock not called | We serialize via pydantic-core (faster, more correct); not via Starlette's path | See migration doc §2 |

## How this table is maintained

Every entry has tagged evidence — when a new failure mode is discovered (or fixed) in a future milestone, add a row pointing at the new test/bench. The rule: **no row without a test that proves the claim.**

The format gives operators a single-page reference and gives the project a way to track which failure modes are actually validated vs assumed.
