# OpenAI-compatible serving on Lancelot — a recipe

> **Lancelot ships the wire primitive, not the OpenAI schema.** This is a
> copy-pasteable recipe for an OpenAI-compatible `/v1/chat/completions` +
> `/v1/models` server built from primitives Lancelot already ships — the
> SSE-of-models framing (`@app.stream(sse=True)` / `sse_adapter`), the bounded
> blocking pool (`pool.offload`), client-disconnect cancellation
> (`cancel_on_disconnect` / `request.is_disconnected()`), and **your own**
> response models. There is no `lancelot.openai_compat`, no first-party
> `ChatCompletionChunk`. The "why" is in [Positioning](#why-lancelot-ships-the-wire-primitive-not-the-openai-models) at the bottom — read it before you ask for a built-in.

The OpenAI streaming wire format is exactly the byte path Lancelot optimizes:
a sequence of `data: <json>\n\n` server-sent events terminated by a literal
`data: [DONE]\n\n`. That framing — and the `[DONE]` sentinel — is what
`@app.stream(sse=True, done_sentinel="[DONE]")` and the underlying
`lancelot.stream.sse_adapter` produce, serialized through the same Rust
pydantic-core seam as every other response. The token-producing loop, the model
resolution, and the OpenAI request/response *shapes* stay 100% yours.

## TL;DR

```python
# pip install lancelot-framework uvicorn[standard]
import lancelot
from lancelot.stream import sse_adapter
from lancelot.responses import StreamingResponse
from lancelot.pool import get_pool
from pydantic import BaseModel

app = lancelot.App()

# ---- YOUR models (or `from openai.types...` — your dependency, not ours) ----
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False

class Delta(BaseModel):
    content: str

class ChunkChoice(BaseModel):
    index: int = 0
    delta: Delta
    finish_reason: str | None = None

class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    model: str
    choices: list[ChunkChoice]

class ChatCompletion(BaseModel):
    id: str
    object: str = "chat.completion"
    model: str
    choices: list[dict]

# ---- YOUR model call. Sync (a local model / a blocking SDK) is fine — the
# blocking pool keeps it off the event loop. Make it a generator of tokens. ----
def run_model(messages: list[ChatMessage]) -> list[str]:
    # Replace with your inference. This stand-in returns two tokens.
    return ["Hello", " world"]

# ---- The endpoint. ONE POST, branching on `stream`, exactly like OpenAI. ----
@app.post("/v1/chat/completions", cancel_on_disconnect=True)
async def chat_completions(req: ChatCompletionRequest, request: lancelot.Request):
    # Offload the (blocking) model call so it never pins the reactor. If your
    # inference is already async, just `await` it instead.
    tokens = await get_pool().offload(run_model, req.messages)

    if req.stream:
        async def chunks():
            for i, tok in enumerate(tokens):
                # Stop the instant the client hangs up — don't keep paying
                # for tokens nobody is reading (see "Cancellation" below).
                if await request.is_disconnected():
                    return
                yield ChatCompletionChunk(
                    id="chatcmpl-1", model=req.model,
                    choices=[ChunkChoice(delta=Delta(content=tok))])
            # The framework appends `data: [DONE]\n\n` on clean exhaustion.
        # sse_adapter frames each model as `data: <json>\n\n` and emits the
        # terminal `data: [DONE]\n\n`; StreamingResponse sets the SSE header.
        return StreamingResponse(sse_adapter(chunks()),
                                 media_type="text/event-stream")

    # Non-streaming: one JSON ChatCompletion (application/json), as OpenAI does.
    return ChatCompletion(
        id="chatcmpl-1", model=req.model,
        choices=[{"index": 0,
                  "message": {"role": "assistant",
                              "content": "".join(tokens)},
                  "finish_reason": "stop"}]).model_dump()


@app.get("/v1/models")
async def list_models():
    # YOUR catalog. Lancelot does not ship a model registry (that's AI logic).
    return {"object": "list",
            "data": [{"id": "my-model", "object": "model",
                      "owned_by": "me"}]}
```

Run it on the FastAPI-parity path (the one with full dependency injection — see
[Why uvicorn](#why-uvicorn-and-not-apprun-for-this)):

```bash
uvicorn yourmod:app --host 0.0.0.0 --port 8000 --workers 4
```

Point any OpenAI client at it:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")

# Streaming — arrives as `data: {...chunk...}\n\n` ... `data: [DONE]\n\n`
for ev in client.chat.completions.create(
        model="my-model",
        messages=[{"role": "user", "content": "hi"}],
        stream=True):
    print(ev.choices[0].delta.content or "", end="")

# Non-streaming — one application/json ChatCompletion
print(client.chat.completions.create(
    model="my-model", messages=[{"role": "user", "content": "hi"}]))
```

The `openai` SDK parses the `data: <json>\n\n` / `data: [DONE]\n\n` stream
natively, because that *is* the OpenAI streaming protocol — which is the whole
point: you only had to produce the bytes, not re-implement the client's parser.

## What each primitive does

| Primitive | Role in the recipe | Ships in |
|---|---|---|
| `sse_adapter(gen)` / `@app.stream(sse=True, done_sentinel="[DONE]")` | Frames each yielded model as `data: <pydantic-core json>\n\n` and auto-emits the terminal `data: [DONE]\n\n`. `Event`/`bytes`/`str` yields pass through unchanged. | `lancelot.stream` (M15 T7) |
| `StreamingResponse(it, media_type="text/event-stream")` | Wraps the framed async-iterator and sets the SSE content-type on a normal `@app.post` route (so the same URL can branch stream vs JSON). | `lancelot.responses` |
| `get_pool().offload(fn, *a, **kw)` | Runs a **blocking** model call on the bounded thread pool so it never pins the event loop; backpressure is bounded (over-cap → `503 Retry-After`). | `lancelot.pool` |
| `cancel_on_disconnect=True` + `await request.is_disconnected()` | Stop server work — and stop paying for upstream tokens — the instant the socket closes. | `@app.post(...)` kwarg + `lancelot.requests.Request` (M15 T6) |

Everything above is wire/transport/serialization plumbing. None of it knows what
a "chat completion" is — *you* supplied the shape.

## Cancellation: stop paying for abandoned generations

Two complementary mechanisms, both already in the recipe:

1. **The streaming body is disconnect-aware by construction.** When the client
   hangs up mid-stream, the next chunk push fails and the generator is left
   suspended (Lancelot's R3 contract: at most one extra chunk after the
   disconnect). You don't have to wire anything for the *stream* itself to stop.
2. **`await request.is_disconnected()` lets you short-circuit upstream work
   between tokens.** Check it before each expensive step (a remote model call, a
   tool invocation) so you stop *generating* — not just stop *sending*.
   `cancel_on_disconnect=True` additionally cancels a non-streaming handler's
   in-flight `await` the moment the socket closes (and yield-dependency teardown
   — DB rollback, file close — still runs).

For a remote/upstream model, make the call through `lancelot.llm.TransportPool`
and bind it to the request: a client disconnect then cancels the in-flight
upstream call (the C2 "stop paying for unwatched tokens" path), and the request's
`traceparent` is forwarded so the model call joins your distributed trace.

## Auth, rate limits, request IDs — use the deps you already have

OpenAI-compatible servers want a bearer key and per-tenant limits. These are
ordinary Lancelot/FastAPI dependencies — nothing OpenAI-specific:

```python
from lancelot import Depends, HTTPException

async def require_key(request: lancelot.Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] not in VALID_KEYS:
        raise HTTPException(status_code=401, detail="invalid api key")
    return auth[7:]

@app.post("/v1/chat/completions", cancel_on_disconnect=True)
async def chat_completions(req: ChatCompletionRequest,
                           request: lancelot.Request,
                           api_key: str = Depends(require_key)):
    ...
```

Per-tenant concurrency is `lancelot.tenant.TenantConcurrencyLimiter`; request-id
echo + `traceparent` propagation are automatic on the ASGI path. Token/cost
accounting (`X-Lancelot-Cost-USD`) falls out of `TransportPool` if you route the
upstream call through it.

## Why uvicorn, and not `app.run()`, for this

`app.run()` (the native Rust `_native.run` server) is for **dependency-free hot
endpoints** — health probes, const-tier responses, handlers that take only the
raw `request`. It does **not** run the Python dependency-injection / body-parsing
layer: a handler there receives only the raw `Request` positionally, so a
declared Pydantic body (`req: ChatCompletionRequest`), `Depends(...)`, and the
magic-injected params do **not** resolve. An OpenAI-compat server needs all of
those, so deploy it under **uvicorn** (or any ASGI server) — the FastAPI-parity
path where DI works. Scale it the way Lancelot scales: **multiprocess**
(`--workers N`), never one process with many threads (the embedded loop is
GIL-serialized). The SSE framing in `sse_adapter` is byte-identical on both
transports — but only the ASGI path gives you the DI an OpenAI endpoint needs.

## Why Lancelot ships the wire primitive, not the OpenAI models

Lancelot owns the **byte path** — transport, serialization, streaming framing,
resilience, observability — for AI/cloud/web/startups. The OpenAI request/response
schema is a **model-API contract**, not a transport format: it's versioned by
OpenAI, it drifts (tools, `response_format`, `logprobs`, audio, ...), and it's
*AI logic*, which is explicitly the user's side of the line. Shipping
first-party `ChatCompletionChunk` / `ChatCompletion` Pydantic shapes would pull
Lancelot toward being a model gateway — duplicating what the `openai` Python
package already gives you, taking on a maintenance treadmill chasing their
schema, and blurring the positioning that keeps the framework small (runtime
dependency: `pydantic` only).

So the split is deliberate, and it mirrors how
[`AutoCommitAPIRoute`](./../plans/2026-05-31-m15-fastapi-ootb-implementation.md)
and other patterns are documented as **recipes, not built-ins**:

* **Lancelot ships** the `data: <json>\n\n` + `[DONE]` framing, the
  Rust-core serialization, the disconnect-cancellation, the bounded offload pool,
  the request-id/trace/cost observability — the parts that are genuinely
  transport and are hard to get right under load.
* **You ship** the OpenAI schema (use `from openai.types...`, or hand-write the
  handful of models you need, or generate them) and the token-producing model
  call — the parts that are your product.

You get an OpenAI-compatible server in ~40 lines, with a streaming path that's
byte-identical across Lancelot's two transports and that any OpenAI client reads
without modification — and Lancelot stays a byte-path framework, not a model API.
