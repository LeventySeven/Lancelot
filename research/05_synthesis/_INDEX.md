# Agent-framework harvest — INDEX (PARKED / exploratory)

**Status: COMPLETE but PARKED.** This `research/` tree (`00_LENS.md`, `02_core/`, `03_teardowns/`, `04_byte_path/`, `05_synthesis/`) is a Verso-grade harvest exploring *"what would a lean, brutal agent framework on Lancelot's byte path look like?"* It was finished for completeness, then **parked**: the active direction is the **byte-path** mine (improving Lancelot-the-HTTP-framework, mined from Robyn / FastAPI / Blazeio). Keep this only as reference if an agent layer is ever revisited.

## What's here
- **`00_LENS.md`** — the harvest lens + Lancelot byte-path inventory + quality bar.
- **`02_core/`** (14 notes) — core transcripts (YC / a16z / DLAI / Stanford / Palantir / MS / Google) + agentic-architecture docs + BAD_GUIDE sections.
- **`03_teardowns/`** (46 notes) — agent frameworks (Pydantic AI, LangGraph, CrewAI, Letta, Autogen, OpenAI/Claude SDK, DSPy, LlamaIndex, Mastra, Vercel AI SDK, ADK, Hermes…) + production agents (Claude Code, Codex, Cursor, Devin/Warp, Manus, Character.AI, Browser-Use, Composio…) + research agents + infra (gateways/memory/evals/durable/serving).
- **`04_byte_path/`** (2 notes) — inventory of Lancelot's existing byte path mapped to agent needs.
- **`05_synthesis/`** (12 layer briefs) — `loop · context · model · tools · memory · evals · streaming · durable · safety · orchestration · positioning · byte_path_integration`.

## The one headline finding (worth keeping)
Lancelot already ships **~70% of an agent framework's plumbing** as battle-tested Rust byte path — `TransportPool` (request/stream/fanout/spawn_child, retry+Retry-After, rate-limit, mTLS, traceparent+idempotency, cost emit), `upstream_metrics`, `canonical` (prompt-cache-stable JSON), `batch`, `stream` (R3 pump+TTFT), `context` ContextVars, `tool`/`mcp`. An agent layer would only BUILD the *brain* (loop, model-shape adapter, context assembly+compaction, typed memory, evals, durable seam, safety gate) — the transport/serialization/streaming half is done and is exactly what pure-Python frameworks can't match. See `05_synthesis/byte_path_integration.md`.
