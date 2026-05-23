# Lancelot Operator Runbook

First-action playbook for the operational events that DO happen in prod. Each row: **signal** (metric/log) → **first action** → **escalation**.

The runbook assumes Lancelot is deployed per [`docs/deploy.md`](../deploy.md): multiprocess `SO_REUSEPORT` behind nginx, OTel metrics scraped by Prometheus, the [`lancelot-red.json`](dashboards/lancelot-red.json) Grafana dashboard imported.

## Quick reference — the 4 emitted metrics

| Metric | Type | Use |
|---|---|---|
| `lancelot.http.requests` | counter | request count by route/method/status |
| `lancelot.http.request.duration` | histogram (sec) | p50/p95/p99 latency |
| `lancelot.http.requests.in_flight` | up-down counter | live concurrency |
| `lancelot.http.stream.ttft` | histogram (sec) | streaming time-to-first-byte |

## Slow handler

**Signal:** `histogram_quantile(0.95, … duration_seconds_bucket)` on the Grafana D panel climbs on one route only. Log line: `request handled` with `duration_ms > X`.

**First action:**
1. Identify the route: filter the duration histogram by `route` label.
2. Check the in-flight counter — if it grew in step, the handler is blocking (sync work in async code, hung upstream).
3. Check upstream — if you have a `lancelot.transport_pool` exposed, the pool's `wait_time` will show backpressure.

**Fix:** if the handler does sync work (DB / CPU), move to `await asyncio.to_thread(...)` or set a per-route `timeout=` to bound the damage.

**Escalation:** if all routes slow simultaneously, jump to **Stuck drain** or **Leak suspect**.

## Stuck drain

**Signal:** during deploy, `lancelot.http.requests.in_flight` doesn't drop to 0 within `LANCELOT_DRAIN_SECS` (default 10s). The process dies with an in-flight count > 0 in the final log line.

**First action:**
1. Increase `LANCELOT_DRAIN_SECS` — if you have legitimately long streams, the default is too short.
2. Look for handler `finally` blocks doing slow work (e.g. flushing a DB transaction). The drain semaphore won't release until the handler's `finally` returns.
3. Check `lancelot.transport_pool` — its drain blocks the main drain.

**Fix:** if a handler can't finish in `LANCELOT_DRAIN_SECS`, bound its in-handler work explicitly (`asyncio.wait_for(...)`). The deploy script then forces SIGKILL only after the drain timeout, which is correct behavior — better a clean drain than an indefinite one.

## Leak suspect (RSS or FD growth over time)

**Signal:** RSS grows monotonically over hours (per soak guard G3) OR FD count grows monotonically over an hour (G4). The Grafana RED panel may look healthy if leaks don't affect throughput yet.

**First action:**
1. Run [`benchmarks/m5/soak.py`](../../benchmarks/m5/soak.py) against the affected service with `--remote-host` so it captures RSS+FD per 30s window.
2. If RSS grows ~linearly: classic memory leak. Common culprits: a global cache that never evicts; a contextvar that escapes; an unclosed `httpx.AsyncClient` per request.
3. If FD grows: open sockets / files that aren't closing. Check `lsof -p <pid>` on the host to see what type of fd is leaking.

**Fix:** depends on culprit. The leak guard is the diagnostic; root-cause is usually in app code, not Lancelot itself. (M4 validated zero leaks under burst+adversarial load — sustained leaks are almost always user code.)

## Batch flood (`@app.batch` only)

**Signal:** route `/api/lookup` (or wherever you use `@app.batch`) returns 503 with `batch full` OR p99 spikes during a burst on one batch key.

**First action:**
1. Check the coalescing ratio: `batch_items / batch_calls` (custom counter if you wire it; see `benchmarks/m5/batch_peer_bench.py` for the pattern).
2. If ratio is at `max_batch` ceiling: bump `max_batch=` and re-deploy.
3. If a single key dominates: that's head-of-line blocking. Re-key (e.g. include user-id in the key) so different keys batch independently.

**Fix:** in extreme bursts, increase `batch_window_ms` to give more requests time to coalesce (tradeoff: higher latency). Don't blindly bump `max_keys` — that's the protection against a key-flood DoS.

## TTFT regression (streaming UX broke)

**Signal:** `lancelot.http.stream.ttft` p95/p99 jumps from baseline (e.g. 50ms → 500ms+). The Grafana TTFT panel goes orange/red.

**First action:**
1. Distinguish: is the regression OURS (Lancelot) or UPSTREAM (LLM provider)?
2. Run `soak.py` with `--route /v1/chat/completions` to confirm reproducibility.
3. If only one route's TTFT is bad: check that specific upstream's response-start latency separately (curl with `-w "%{time_starttransfer}"`).
4. If all routes regress: check the in-flight gauge — if it's sustained-high, you're CPU-pegged and the asyncio loop is starving the first-byte emit.

**Fix:** if upstream is slow, that's not Lancelot's bug; route around. If CPU-pegged, scale horizontally (more processes via `SO_REUSEPORT`).

## Upstream dead (TransportPool sees all upstream errors)

**Signal:** sudden spike in 5xx for routes that proxy to an LLM. `lancelot.http.requests{status="5xx"}` jumps. `transport_pool.cancel_event` fires repeatedly.

**First action:**
1. Check the LLM provider's status page.
2. Check the TransportPool's circuit-breaker state if you wired one.
3. Drop the rate (`@app.batch` window down, `max_batch` down — reduces upstream-call rate; or temporarily redirect all to a fallback).

**Fix:** Lancelot's C2 cancellation (M4) means client disconnects DO propagate to cancel the upstream call — so you're not paying for unwatched tokens. Verify this with `transport_pool` metrics if you have them.

## 413 flood (body-cap saturation)

**Signal:** sustained `lancelot.http.requests{status="413"}` at rate >> normal. Log: `body too large`.

**First action:**
1. Verify the legitimate request size — sometimes a client started sending uncompressed bodies. The default body cap is 16 MiB.
2. Increase `App(max_body_bytes=...)` if legitimate.
3. If adversarial (a misconfigured/abusive client), block at nginx via `client_max_body_size` BEFORE it hits Lancelot.

**Fix:** the 413 is doing its job (M4 V1.2 validated). The runbook concern is identifying the SOURCE.

## Connection cap (overload, 100% in-flight)

**Signal:** `lancelot.http.requests.in_flight` pinned at `LANCELOT_MAX_CONNECTIONS` (default 1024). New connections see kernel-backlog drop (clients get connection-reset).

**First action:**
1. This is by design (M4 V1.3 — overload protection). Lancelot is NOT crashing.
2. Scale horizontally: more processes via `SO_REUSEPORT`. nginx upstream backlog should pick up the slack.
3. If you must absorb the burst, increase `LANCELOT_MAX_CONNECTIONS` — at the cost of higher memory ceiling per process.

**Fix:** scaling. The cap is a feature, not a regression.

## Slow-loris (header dribble attack)

**Signal:** lots of TCP connections in `ESTABLISHED` but never advancing to a request. `lancelot.http.requests` doesn't increase. The Lancelot log shows: `header_read_timeout`.

**First action:**
1. Lancelot drops these at `LANCELOT_HEADER_TIMEOUT_MS` (default 15000 = 15s).
2. If the attack volume exceeds `LANCELOT_MAX_CONNECTIONS`, it WILL squeeze legitimate clients out (the M4 V1.1 protection is per-conn, not network-level).
3. Block at nginx / the LB: enable nginx `limit_conn` + `limit_req_zone` rules, or use a WAF.

**Fix:** Lancelot's job is per-conn deadline; defense-in-depth at the LB is the right operational answer.

## Reading logs

Default Lancelot log line shape:

```
{level} {timestamp} {request_id} {route} {method} {status} {duration_ms} {extra}
```

`request_id` is the `X-Request-ID` header echoed for trace stitching. The ContextVar (`lancelot.context.request_id`) means `request_id` is correct across the asyncio handler + the thread pool path — verified by M4 V3.2 under 200-concurrency.

If you set `LANCELOT_LOG_FMT=json`, the same fields emit as JSON for trivially-piped log ingestion (Loki/Datadog/etc.).

## Escalation contacts

(Project-internal — fill in for your team.)

| Tier | Contact | Hours | Escalate when |
|---|---|---|---|
| L1 (on-call) | … | 24/7 | any sustained alert > 5 min |
| L2 (sr eng) | … | bus hours | L1 root-causes; needs platform action |
| L3 (project) | file an issue | — | a Lancelot bug is suspected after L2 |
