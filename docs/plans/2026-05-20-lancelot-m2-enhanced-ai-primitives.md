# Lancelot M2 — Enhanced AI Primitives + Selective Rust Hot-Path

> **Identity (sharpened per user)**: a faster FastAPI that's AI-first WITHOUT restricting anyone — users write literal FastAPI code, Rust kicks in only where it pays off (Robyn-style), AI is additive opt-in. No new framework to learn. No graphs/DSLs/sandboxes/vector-stores/agent-base-classes (all CUT per strict synthesis).

**Source**: the strict 3-agent research synthesis that audited the prior 7-primitive draft. Agent C cut 2 and trimmed 3 of the 7. Agent A confirmed: do NOT ship new user surface for things FastAPI already covers; absorb only what real product code re-types ≥5×. Agent B mapped where Rust pays off vs where Python is fine.

## The 5 surviving AI primitives (ship)

| # | Primitive | Surface (≤6 lines) | Closes (real product evidence) |
|---|---|---|---|
| **P1** | `app.gather(*coros, concurrency=N)` | one helper, no rps cap | 5+ products hand-roll `Semaphore + gather(return_exceptions)` |
| **P2** | `app.llm` outbound transport pool + retry policy | `pool.request("POST", url, json=...); pool.stream(...)` keyed per-host; POST never retried; `Retry-After`-honoring retry on idempotent verbs only | Supermemory Oct-2025 incident; every product reinvents this |
| **P3** | Auto OTel GenAI spans + cost event on every `app.llm` call | zero user code; framework emits `{prompt_tokens, completion_tokens, cached_tokens, cost_usd, model}` | Laminar's entire product; 8+ products with per-model cost tables |
| **P4** | `@app.stream("/...")` typed event sugar | `async def chat(req) -> Stream[Event]: yield Event.delta(tok); yield Event.done()` — framework owns SSE framing + cancellation propagation via `request.cancelled` | 9+ products hand-roll `text/event-stream` framing |
| **P5** | `@app.tool` → JSON-Schema + MCP mount | `@app.tool(name, scopes=[…]) def search(q: str) -> list[Hit]: ...` — reuses existing `Dependant`+Pydantic compiler; same code emits OpenAPI route schema AND MCP tool schema | 10+ products (Cursor 80, Codex 20, Claude 21 tools); Tavily ships a SEPARATE codebase as their MCP server |

## What is CUT (the no-bullshit sieve survivors)

- **Provider DI (`app.bind(LLM, ...)`)** — FastAPI's `Depends()` already does this (15+ existing product usages). Net new value ≈ 0.
- **cancel_token threading through SDKs** — zero `is_disconnected` calls in product code; demote to docs page on `request.cancelled` awaitable.
- **Circuit breaker** — no product code defines breaker thresholds; trim from retry policy.
- **`resumable=True` SSE replay** — zero product citations; cut from `@app.stream`.
- **`rps=cap`** on `app.gather` — only 1 product cites it; cut.
- **`@app.workflow` durable replay** — user said "stay out of M2."
- **Opinionated `@app.agent` loop / `AgentStep` enum / `max_steps`** — restrictiveness 4/5; users compose manually.
- **Vector stores / sandboxes / fine-tuning / semantic cache / observability UI / agent base classes** — each its own product; never framework.

## The Rust hot-path swaps (transparent — user code unchanged)

These ride alongside the AI primitives. From Agent B's RUST-WORTH-IT (every item cites measurement or a documented Robyn/Blazeio decision):

| # | Swap | Lever | Source |
|---|---|---|---|
| **R1** | Router const-tier (`/livez`, `/openapi.json`, static-shape routes) | ~10–50× | `SYNTHESIS.md:31` |
| **R2** | Lazy request proxy (Granian-style) | "throughput doubling or tripling" | `design/01:120` |
| **R3** | Streaming Rust pump | kills Robyn's per-token spawn+GIL | `deep/04:23` |
| **R4** | Single-pass `validate_json(bytes)` (FFI wiring) | kills request.json()→loads→validate detour | `SYNTHESIS.md:66` |
| **R5** | Untyped/SSE Rust serialize (`orjson` in Rust) | kills `jsonable_encoder` Python recursion | `SYNTHESIS.md:35,89` |
| **R6** | `header_read_timeout` + connection cap | closes slow-loris DoS | `design/01:400` |
| **R7** | `catch_unwind` at FFI seam | crashed-worker respawn | `deep/04:17` |

**Architecture rule**: Rust swaps are *implementation* swaps under unchanged user surface (Lancelot user types `EventSourceResponse(...)`, gets Rust pump bytes-out). Never new user API.

## Sub-plan order (leverage × dependency)

- **M2-P1**: `app.gather(*coros, concurrency=N)` — no deps; 1 commit; closes ~5 product patterns immediately
- **M2-P2**: `app.llm` transport pool + retry policy (POST-never-retried, Retry-After-aware; pool keyed per-host) — foundation for P3
- **M2-P3**: Auto OTel GenAI spans + cost event on `app.llm` — highest leverage primitive
- **M2-P4**: `@app.stream` typed `Stream[Event]` sugar + `request.cancelled` awaitable (no `resumable=`)
- **M2-P5**: `@app.tool` → JSON-Schema + MCP Streamable-HTTP server mount (Robyn `mcp.py` shape, 5 gaps closed per `robyn/03:553-560`)
- **M2-R1..R7**: Rust hot-path swaps as a parallel track. Each one is a Rust-side change with a benchmark + a no-user-surface-change invariant.

## Discipline (same as M0/M1)

Each sub-plan: TDD → real-FastAPI byte-differential oracle (where applicable) → review agent (anti-cheat + behavior-fakery audit) → tag → push → memory. Each Rust swap: criterion benchmark + clippy `-D warnings` + unsafe-checker audit. No vendored-test edits ever. Floor only-rises.

The ultimate recheck after M2 verifies: the AI primitives are minimal (≤BackgroundTasks surface each), the Rust swaps are transparent (user code unchanged), the conformance floor hasn't regressed, and no behavior fakery was introduced.
