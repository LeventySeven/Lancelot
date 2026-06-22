# Durable Execution — Tactical↔Durable Seam
## Source cluster: PREFECT.md · COGNITION_INFRA.md · DISPATCH.md · TEMPORAL_RE_NOTES.md (missing)

**Date:** 2026-06-22  
**Scope note:** TEMPORAL_RE_NOTES.md did not exist at the assigned path — skipped per task instructions. The three present files (PREFECT, COGNITION_INFRA, DISPATCH) are _not_ purpose-built durable execution teardowns; they are FastAPI server architecture (PREFECT, DISPATCH) and Cognition's VM infrastructure (COGNITION_INFRA). Signal on durable execution is indirect but real. Every claim below is labeled KNOWN / INFERRED per the source label convention.

---

## 0. The honest signal map

| Source | Durable exec relevance | What it actually covers |
|---|---|---|
| PREFECT.md | Low — this teardown covers Prefect's REST API server only, not the Prefect orchestration engine (flow definitions, task retries, state machine, checkpointing). The orchestration primitives live in `prefect/engine/`, explicitly out of scope. | `PrefectAPIRoute`, `PrefectRouter`, `session_context`, `Docket`, `SubprocessASGIServer` |
| COGNITION_INFRA.md + OTTERLINK.md | **High** — Cognition's VM hypervisor (otterlink) is a durable execution substrate. blockdiff snapshot chains are the physical implementation of checkpoint/resume/rollback for a long-running agent process. | blockdiff CoW snapshots, sleep/wake, crash recovery, multi-agent coordination rule ("writes stay single-threaded") |
| DISPATCH.md | Low — incident management REST API. Background tasks via `BackgroundTasks` + `@background_task` decorator, but no event sourcing or durable state. | `BackgroundTasks`, `@scheduled_project_task`, post-response flows |

---

## 1. The blockdiff snapshot chain — physical implementation of agent checkpoint/resume/rollback

**Source:** COGNITION_INFRA.md §1, §C.7; OTTERLINK.md §O.3, §O.12; `github.com/CognitionAI/blockdiff/src/main.rs` (KNOWN)

### 1.1 The mechanism (not vibes)

The primitive: `blockdiff create output.bdiff target.img --base base.img`. Produces a compact diff file. The format:

```
BDIFFv1\0   (8 bytes magic)
target_size (8 bytes LE)
base_size   (8 bytes LE)
n_ranges    (8 bytes LE)
[DiffRange {logical_offset: u64, length: u64}]  × n_ranges
[data blocks at 4 KiB alignment]
```

Constants: `MAGIC = b"BDIFFv1\0"`, `BLOCK_SIZE = 4096`.  
Source: `main.rs:54,57`.

**Why it is fast:** Uses Linux `FS_IOC_FIEMAP` ioctl to read the VM disk's physical extent map instead of reading data. On an XFS CoW filesystem, a `cp --reflink`'d file shares physical blocks (flagged `SHARED` in the fiemap) with its base. blockdiff reads the fiemap metadata — no I/O on the data itself — and writes only the ranges whose physical addresses diverged from the base. A 128 GB disk image with 100 MB of changes generates a diff in milliseconds. The blog reports "~8ms for a reflink copy" vs 24s without. Snapshot creation: ~200ms for 20GB, vs EC2's 30+ minutes (200× speedup). KNOWN: `cognition.ai/blog/blockdiff`.

**Apply uses `copy_file_range` (in-kernel zero-copy):** `main.rs:315`. For CoW filesystems, `copy_file_range` can do reflink (pointer manipulation in filesystem metadata, no data movement). KNOWN: `main.rs:315`.

**Safety gate before snapshot:** `main.rs:123–183` checks every extent for unsafe flags (`DELALLOC`, `UNWRITTEN`, `NOT_ALIGNED`, `UNKNOWN`, `ENCODED`, `DATA_ENCRYPTED`). Any of these aborts the snapshot with an error. `DELALLOC` (pending kernel-buffered writes) is mitigated by `fsync` before invoking blockdiff (`main.rs:364`). KNOWN.

### 1.2 Snapshot stacking = rollback without a state machine

Snapshots chain: base.img → snap1.bdiff → snap2.bdiff → snap3.bdiff. Each layer is a diff against the previous. To roll back to snap1: `blockdiff apply snap1.bdiff target.img --base base.img`. Many layers on top of one base = in-session rollback without any external event log or state machine. KNOWN: COGNITION_INFRA.md §1 ("Stack many incremental snapshots on top of each other to enable in-session rollback").

**Lancelot byte-path implication:** This is below Lancelot's layer — it is filesystem/hypervisor infrastructure. Lancelot's `lancelot.context` ContextVars and `lancelot.upstream_metrics` accumulate per-request cost/token state; they do NOT persist across process restarts. If a Lancelot-hosted agent needs checkpoint/resume, it needs to serialize and externalise that ContextVar state before a shutdown — blockdiff handles the whole-process layer, not the application layer.

---

## 2. Sleep/wake = durable promise at the infrastructure layer

**Source:** OTTERLINK.md §O.12; COGNITION_INFRA.md §C.8; `cognition.ai/blog/what-we-learned-building-cloud-agents` (KNOWN)

Cognition's sessions can pause waiting for CI/review/webhook for hours or days, then resume in under one second. The mechanism (KNOWN from Cloud Agents blog verbatim):

> "snapshotting full machine state at the hypervisor level — **memory, process trees, and filesystem**"

Concretely (INFERRED from Firecracker API + blockdiff source):
1. Sleep trigger: agent calls `<sleep_until_completion>` (or orchestrator detects idle).
2. `KVM_PAUSE_VM` freezes all vCPUs.
3. Memory serialized to `mem.snap` (sparse mmap or Firecracker-style savefile).
4. Disk state blockdiff'd against last checkpoint → written to S3.
5. VM destroyed (no resource consumption while sleeping).
6. Wake trigger: webhook / schedule / user click.
7. Restore: host picks `mem.snap` + latest disk bdiff, runs `blockdiff apply`, VMM loads memory snapshot via KVM, resumes. Sub-second after disk is hot.

OTTERLINK.md §O.12: "The limit isn't storage cost but wake-up latency."

**The caveats that must be solved for production (INFERRED, OTTERLINK.md §O.18.3):**
- DMA in flight must be drained before snapshot.
- Guest TSC/RTC must be reset after resume (wall-clock jump breaks `Date.now()`).
- All TCP sockets die on resume (remote peers have timed them out). The agent harness must reconnect. Devin's per-tool-call independence makes this tractable.

**HITL (human-in-the-loop) as a sleep event:** Sleep/wake IS the durable promise mechanism at this layer. There is no explicit "await human_approval()" primitive in the public Cognition API — human interaction arrives via `POST /v3/organizations/{org}/sessions/{id}/messages` while the session is sleeping. The session wakes, reads the message, and continues. KNOWN: `api.devin.ai/v3` endpoint catalog, COGNITION_INFRA.md §C.5.

**Lancelot byte-path implication:** Lancelot's byte path is transport-layer — it has no sleep/wake primitive. A Lancelot-based agent framework would implement HITL differently: either (a) a `@app.stream` SSE channel that the agent suspends on (the agent's Python async-gen is suspended, the Rust pump keeps the SSE connection alive via keepalive pings per `stream_interval`) or (b) the agent process terminates and a separate queue (Redis/Postgres) holds the pending approval, with a new request resuming. Lancelot's `@app.stream` SSE keepalive + `lancelot.context` ContextVars survive within a request lifecycle; they do NOT persist across process restarts.

---

## 3. Multi-agent coordination: "writes stay single-threaded"

**Source:** COGNITION_INFRA.md §C.4; `cognition.ai/blog/multi-agents-working` (KNOWN verbatim)

> "multiple agents contribute intelligence to a task while writes stay single-threaded"

The rule (KNOWN, multi-agents-working blog):
- Parallel agents can READ concurrently (search, retrieval, critique).
- Only ONE agent holds the write lock on any file/PR/resource at a time.
- Siblings work in disjoint scopes (different files/PRs/tasks).

The review agent pattern (KNOWN, same blog, averages 2 bugs/PR, ~58% severe):
> "the review agent intentionally receives no prior context, forcing it to reason backward from implementation alone"

The anti-patterns explicitly rejected (KNOWN):
- Unstructured agent swarms with arbitrary negotiation.
- Parallel writers touching the same files — causes merge conflicts and incoherent commits.
- "Map-reduce-and-manage" alternatives lacking a single source of truth.

**Durable execution implication:** The "writes stay single-threaded" rule is what makes checkpoint/resume safe in a multi-agent system. If multiple agents write the same filesystem simultaneously, snapshot consistency breaks. Cognition's per-VM isolation (each agent is its own microVM) enforces this at the hardware level: inter-VM communication only via the orchestrator, no shared filesystem.

**Lancelot byte-path implication:** `TransportPool.fanout` and `spawn_child` are the byte-path analogues — they do outbound LLM fan-out, not shared-state writes. Lancelot doesn't have a filesystem/shared-state primitive. The "writes stay single-threaded" rule applies to the application layer above Lancelot.

---

## 4. Session trajectory as an append-only event log

**Source:** COGNITION_INFRA.md §C.8 (INFERRED + KNOWN from API)

The Devin session trajectory storage (KNOWN: API docs; INFERRED: S3+Postgres):

| Layer | Store | Retention | Access |
|---|---|---|---|
| Session event log | S3 (append-only) | Persistent | `GET /v3/organizations/{org}/sessions/{id}` |
| Session metadata | Postgres | Persistent | Same endpoint |
| Trajectory content | S3 | Per-session | Readable by parent Devins for `Manage Devins` |

COGNITION_INFRA.md §C.8 (INFERRED): "S3 (event log) + Postgres (session metadata)."

The parent Devin can "read the full trajectories of its managed Devins" (KNOWN: `cognition.ai/blog/devin-can-now-manage-devins`). This means the event log is readable, queryable, and structured — not just a raw dump.

**What this is NOT:** This is not event sourcing in the Temporal/EventStore sense. There is no replay of events to reconstruct state. The trajectory is an audit log + context feed, not a source of truth for state reconstruction. State reconstruction is blockdiff-based (the VM disk snapshot chain), not event-replay-based.

---

## 5. Prefect's background work primitives (limited durable-exec relevance)

**Source:** PREFECT.md §7, §3.4 (KNOWN)

Prefect's API server uses an in-process `Docket` (Redis-or-memory-backed task queue) + a `background_worker` service, both owned by the lifespan `AsyncExitStack`:

```python
# api/server.py:903-937
async with AsyncExitStack() as stack:
    docket = await stack.enter_async_context(Docket(name=..., url=..., execution_ttl=timedelta(0)))
    await stack.enter_async_context(background_worker(docket, ...))
    api_app.state.docket = docket
```

`execution_ttl=timedelta(0)` — tasks have no execution time limit here (the server-side queue is just a dispatch mechanism; actual flow execution happens in separate Prefect workers, out of scope for this teardown).

The `Docket` is exposed as a FastAPI `Annotated` dependency:
```python
# dependencies.py:201-205
Docket = Annotated[Docket_, Depends(lambda req: req.app.state.docket)]
```

**What this teardown DOES NOT cover:** The actual Prefect durable execution primitives (flow state machine, `RUNNING/PAUSED/CANCELLED/COMPLETED/FAILED`, task retries with exp-backoff, checkpointing via result serialization to storage backends, `pause_flow_run()` for HITL) live in `prefect/engine/` and `prefect/tasks/` — explicitly out of scope for the REST API teardown. The correct sources for those are the Prefect Python client library, not the server teardown.

**Honest flag:** The PREFECT.md source assigned here is the wrong source for durable execution primitives. It is the FastAPI server layer. The durable orchestration engine is a separate codebase that was not teardown'd here.

---

## 6. Dispatch's `@background_task` + `@scheduled_project_task` — limited relevance

**Source:** DISPATCH.md §14, §15 (KNOWN)

Dispatch's `@background_task` decorator opens a fresh schema-bound session from `organization_slug` (since `request.state` is gone post-response) and runs a flow:

```python
# decorators.py:96-133
@background_task
def incident_create_flow(*, organization_slug, incident_id, db_session=None):
    # decorator sees no db_session → rebuilds from organization_slug
    # On exception: rollback + raise; finally: close
```

The `@scheduled_project_task` runs once-per-(org, project) for every scheduled job (cron-style), using `engine.execution_options(schema_translate_map={None: f"dispatch_organization_{slug}"})` per tenant.

**What this is NOT:** These are fire-and-forget background tasks with transaction discipline — not event-sourced, not checkpointed, not resumable. A crash mid-flow loses the state. No idempotency key, no at-most-once or exactly-once semantics, no retry policy beyond "the request middleware rolls back the DB transaction."

---

## 7. What the assigned sources DO NOT contain (be honest about gaps)

The focus topic was: "event-sourced checkpoint/resume, the step() bracket (side effects skipped on resume), sync-vs-async durability, durable promise for HITL, submit/attach/cancel, seq via DB IDENTITY not MAX+1."

None of the three present sources contain these primitives:

| Primitive | Source coverage |
|---|---|
| Event-sourced state machine | **Absent.** The Prefect teardown covers the REST API only. |
| `step()` bracket (skip side effects on replay) | **Absent.** None of the three sources implement this. |
| Sync-vs-async durability default | **Absent.** Dispatch's flows are fire-and-forget (no durability). Cognition's are VM-snapshot-based. |
| Durable promise / HITL await | **Partially present** via Cognition's sleep/wake mechanism (§2 above). Not an explicit promise API. |
| DB IDENTITY for sequence (not MAX+1) | **Absent.** Prefect uses `uuid4`/`uuid7` PKs, not sequences for event ordering. |
| `submit/attach/cancel` API shape | **Absent.** Cognition's `POST /v3/.../sessions` is the closest (submit), with `GET` for attach, and `message_user`-initiated cancel. Not a typed durable-execution API. |

**The relevant teardowns for these primitives** (exist in the teardown corpus but were not assigned):
- `LANGGRAPH.md` — event sourcing for agent graphs, checkpoint/resume, `interrupt()` for HITL
- `CONDUCTOR.md` — if present, Orkes/Netflix Conductor state machine
- `MODAL.md` — checkpointed async functions
- `MASTRA.md` — workflow step() / suspend() / resume() pattern

---

## 8. Reusable constants and mechanisms extracted

### 8.1 blockdiff constants (KNOWN, `main.rs`)

```rust
const MAGIC: &[u8; 8] = b"BDIFFv1\0";
const BLOCK_SIZE: usize = 4096;
```

The unsafe extent flags that abort a snapshot (`DELALLOC`, `UNWRITTEN`, `NOT_ALIGNED`, `UNKNOWN`, `ENCODED`, `DATA_ENCRYPTED`) at `main.rs:123-183`.

### 8.2 Cognition snapshot timing (KNOWN, blog posts)

- Reflink copy of 128 GB image: ~8ms (XFS, `cp --reflink=always`)
- Snapshot creation (20GB changed data): ~200ms
- EC2 snapshot baseline: 30+ minutes
- Data transfer after snapshot: ~2 GB/s (`copy_file_range` zero-copy on NVMe)
- VM resume from snapshot: "sub-second after disk is hot"

### 8.3 Prefect `is_client_retryable_exception` (KNOWN, `server.py:1126-1152`)

The exact SQLite/asyncpg predicate for distinguishing retryable (→ 503) from permanent (→ 500) DB errors. The load-bearing subtlety: `OperationalError` without `SQLITE_BUSY`/`SQLITE_BUSY_SNAPSHOT` must explicitly `return False` before the broad `DBAPIError` branch, or a syntax error becomes a 503. See PREFECT.md §D4 for verbatim code.

### 8.4 Multi-agent coordination constants (KNOWN, SWE-grep blog)

SWE-grep's tool-call budget: "up to 8 parallel tool calls per turn in a maximum of 4 turns (3 turns of exploration and 1 turn for the answer)." Maximum: 32 tool calls per retrieval task. KNOWN: `cognition.ai/blog/swe-grep`.

---

## 9. The byte-path lens: what Lancelot already provides vs. what it lacks

| Durable exec primitive | Lancelot status |
|---|---|
| Within-request state (ContextVars: cost, tokens, agent_id, trace_id) | **HAS** — `lancelot.context` ContextVars, accumulated across a request |
| Streaming HITL (suspend agent, keep SSE alive) | **PARTIALLY HAS** — `@app.stream` SSE keepalive + `[DONE]` marker; the agent's async-gen suspends while the pump holds the connection. Works for within-request HITL only. |
| Outbound fan-out (parallel LLM calls) | **HAS** — `TransportPool.fanout(first_n, deadline, cancel_laggards)` |
| Child agent context propagation | **HAS** — `TransportPool.spawn_child(parent_request)` snapshots traceparent + request_id + agent_id |
| Across-request durable state | **LACKS** — no persistence layer. ContextVars die with the request. |
| Checkpoint/resume | **LACKS** — no snapshot/replay primitive. |
| Event log / trajectory | **LACKS** — no append-only event store. |
| HITL durable promise | **LACKS** — no pause/resume across requests. |
| Step() bracket (skip side effects on replay) | **LACKS** — no replay concept. |

**The gap is the application layer above the byte path.** Lancelot is the transport substrate (byte path). Durable execution sits one layer up: the agent framework needs its own checkpoint store (Postgres + S3, Redis, or a dedicated durable execution engine like Temporal/Mastra) and serializes `lancelot.context` ContextVar state into it at step boundaries. Lancelot would serve the HTTP surface for the `submit/status/cancel` API and the SSE stream for real-time HITL notifications; the durability itself is the user's responsibility (per the positioning lock: "AI logic stays on the user side — LangGraph, agent loops, vector stores, prompt orchestration. We own the byte path; the user owns the agent").

---

## 10. Anti-patterns (from the source evidence)

1. **Parallel writers on shared state** — explicitly rejected by Cognition (KNOWN, multi-agents-working blog): causes merge conflicts and incoherent commits. The fix: writes single-threaded, reads parallel.
2. **VM-level snapshot as a substitute for application-level event sourcing** — Cognition's blockdiff is powerful but not event-sourced. If the VM disk is corrupted or the snapshot chain breaks, there is no replay. A crash mid-write leaves the VM in an undefined state that `blockdiff apply` cannot recover from (it applies the diff but doesn't know what the application was doing). For agent frameworks that need replay, blockdiff is NOT the right primitive.
3. **Using `BaseHTTPMiddleware` for streaming contexts** — KNOWN: PREFECT.md §D3 (`RequestLimitMiddleware`). `BaseHTTPMiddleware` buffers the response, breaking `StreamingResponse`. Use raw-ASGI `__call__(scope, receive, send)` + `anyio.CapacityLimiter` instead.
4. **`MAX+1` sequence generation** — not directly in the sources, but Prefect uses `uuid7` (time-ordered UUID) for log/event streams (`bases.py:196: id: UUID = Field(default_factory=uuid7)`). This is the correct alternative to `MAX+1`: the timestamp is embedded in the UUID so ordering is preserved without a DB sequence lock. KNOWN: PREFECT.md §6b.2.
5. **In-process background tasks for durable work** — Dispatch explicitly notes (DISPATCH.md §7): "`BackgroundTasks` runs in the same process/event-loop as the server (not a separate worker queue). For Netflix-scale durability you'd want Celery." Dispatch accepts in-process tasks as a "good enough, lower-ops" choice. The anti-pattern: treating `BackgroundTasks` as durable — a process crash drops all pending tasks.

---

## Summary

The three present sources contain:
- One strong durable-exec-adjacent primitive: **blockdiff snapshot chains** (Cognition/OTTERLINK) — the physical implementation of checkpoint/rollback/sleep-wake for long-running agents.
- One coordination rule with real production evidence: **writes stay single-threaded** (Cognition) — the invariant that makes consistent snapshots safe.
- One HTTP-layer durable-execution support pattern: **`uuid7` for time-ordered event IDs** (Prefect) + **`is_client_retryable_exception` predicate** (Prefect) for 503-vs-500 status semantics.

The canonical durable execution primitives (event-sourced step() bracket, sync-default, durable promise) are absent from these sources. TEMPORAL_RE_NOTES.md (missing) was the primary intended source for those. The closest available teardowns in the corpus for those primitives are LANGGRAPH.md and MASTRA.md.
