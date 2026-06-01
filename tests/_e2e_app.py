"""End-to-end fixture app for `tests/test_e2e_app.py`.

A representative AI-startup / LLM-proxy backend that wires together MANY Lancelot
features at once so the E2E suite can prove they cohere when driven over real
HTTP (NOT TestClient). It is importable (`tests._e2e_app:app` — uvicorn ASGI
surface) AND directly runnable (`python tests/_e2e_app.py` ends in `app.run()` —
the native Rust server surface).

DUAL-SURFACE DESIGN (respects the verified native-server constraints):
  * Native `app.run()` calls every handler as `handler(request)` — ONE positional
    arg, NO DI, NO body-parsing. So every handler here takes `request` FIRST and
    every other param is DEFAULTED (`body: ChatReq = None`, `tenant=Depends(...)`),
    which makes `handler(request)` legal on native while uvicorn still injects the
    parsed Pydantic body + resolved deps by name. A handler that needs the body
    detects the native path by `body is None` and parses it itself via
    `await request.json()`.
  * Native DOES drive the lifespan (M18) + serves the const tier; it does NOT do
    DI / body-validation. uvicorn does the full stack.

Side channels (so the out-of-process test can observe lifecycle):
  * LANCELOT_E2E_STATEFILE — append-only log of lifespan ENTER/EXIT, sub-app
    ENTER/EXIT, and lifetime-task HEARTBEAT lines (loop-identity proof).
"""
import asyncio
import contextlib
import os
import sys

import pydantic

import lancelot
from lancelot import Depends, PageParams, Request
from lancelot.auth import StaticTokenGate
from lancelot.compress import GZipMiddleware
from lancelot.exceptions import ProblemDetail
from lancelot.ratelimit import RateLimit

_STATEFILE = os.environ.get("LANCELOT_E2E_STATEFILE")


def _record(line: str) -> None:
    """Append a lifecycle marker to the side-channel file (best effort)."""
    if not _STATEFILE:
        return
    with open(_STATEFILE, "a") as f:
        f.write(line + "\n")
        f.flush()


# --- request / response models (the uvicorn full-stack validation path) -------
class ChatReq(pydantic.BaseModel):
    prompt: str
    max_tokens: int = 16


# --- lifecycle: root lifespan async-CM -----------------------------------------
@contextlib.asynccontextmanager
async def lifespan(app):
    # On enter: flip the readiness flag the /readyz-style probe reads, and a
    # marker the FIRST request can observe (the classic startup footgun).
    app.state.ready = True
    app.state.boot_id = "BOOT-OK"
    _record("ROOT_ENTER")
    try:
        yield
    finally:
        app.state.ready = False
        _record("ROOT_EXIT")


# --- lifecycle: a mounted sub-app with its own lifespan ------------------------
@contextlib.asynccontextmanager
async def sub_lifespan(sub):
    _record("SUB_ENTER")
    try:
        yield
    finally:
        _record("SUB_EXIT")


app = lancelot.App(lifespan=lifespan, title="Lancelot E2E", version="9.9.9")
sub = lancelot.App(lifespan=sub_lifespan)


@sub.get("/info")
async def sub_info(request: Request):
    return {"sub": "ok"}


app.mount("/sub", sub)


# --- lifecycle: a supervised lifetime daemon writing a heartbeat ---------------
@app.lifetime_task
async def heartbeat():
    try:
        while True:
            _record("HEARTBEAT")
            await asyncio.sleep(0.05)
    finally:
        _record("HEARTBEAT_FINALLY")


# --- the const (prebaked) tier — native server serves this zero-GIL ------------
# Under uvicorn the const tier does not exist, so register the equivalent route
# for that surface too (so /livez answers 200 on BOTH surfaces).
app.const_route("GET", "/livez", b'{"status":"ok"}', "application/json")


@app.get("/livez")
async def livez(request: Request):
    return {"status": "ok"}


# --- a readiness probe that reflects the lifespan-set state --------------------
@app.get("/readyz-app")
async def readyz_app(request: Request):
    # Reads the SAME app object the lifespan mutated. If startup didn't run,
    # app.state.ready is unset -> AttributeError -> 500 (the bug this guards).
    return {"ready": bool(app.state.ready), "boot_id": app.state.boot_id}


# --- the chat endpoint: Pydantic body + Depends (uvicorn), raw on native -------
def current_tenant(request: Request) -> str:
    """A dependency reading a header. Under uvicorn this is injected via Depends;
    on native the handler resolves the tenant itself (no DI there)."""
    return request.headers.get("x-tenant-id", "anon")


def _coerce_chat(request: Request, body):
    """Return a (prompt, max_tokens) pair from EITHER the injected Pydantic body
    (uvicorn) or the raw request JSON (native, where body is None). Raises
    ProblemDetail on the native path for a malformed body so even there a client
    gets a structured error rather than a 500."""
    if body is not None:                       # uvicorn: already validated
        return body.prompt, body.max_tokens
    # Native path: parse + lightly validate ourselves.
    try:
        data = request.json()
    except Exception:                          # noqa: BLE001
        raise ProblemDetail(400, title="Bad Request",
                            detail="body is not valid JSON")
    if not isinstance(data, dict) or "prompt" not in data:
        raise ProblemDetail(422, title="Unprocessable Entity",
                            detail="missing 'prompt'")
    return data["prompt"], int(data.get("max_tokens", 16))


@app.post("/v1/chat")
async def chat(request: Request, body: ChatReq = None,
               tenant: str = Depends(current_tenant)):
    prompt, max_tokens = _coerce_chat(request, body)
    # On native, `tenant` came from Depends's default object (not resolved), so
    # recompute it from the raw request there.
    if not isinstance(tenant, str):
        tenant = current_tenant(request)
    # A trivial deterministic "completion" — AI logic stays user-side; we just
    # echo so the test can assert the byte path end-to-end.
    return {"model": "echo-1", "tenant": tenant,
            "completion": f"you said: {prompt}", "max_tokens": max_tokens}


# --- a ProblemDetail (RFC 9457) error path ------------------------------------
@app.get("/v1/boom")
async def boom(request: Request):
    raise ProblemDetail(
        status=402, type="https://errors.example/quota",
        title="Quota Exceeded", detail="monthly token budget exhausted",
        instance="/v1/boom", extensions={"balance_usd": 0.0})


# --- pagination with a DoS cap (oversize -> 422) ------------------------------
_ROWS = [{"id": i, "name": f"item-{i}"} for i in range(500)]


@app.get("/v1/items")
async def list_items(request: Request, page=Depends(PageParams(max_size=50))):
    # On native, `page` is the Depends() default object (DI didn't run), so
    # resolve PageParams ourselves there.
    if not hasattr(page, "offset"):
        page = await PageParams(max_size=50)(request)
    window = _ROWS[page.offset:page.offset + page.size]
    return {"page": page.page, "size": page.size, "items": window}


# --- an SSE streaming endpoint (M16/M17 framing + terminal sentinel) -----------
class Delta(pydantic.BaseModel):
    index: int
    text: str


@app.stream("/v1/stream", sse=True, done_sentinel="[DONE]", error_frame=True)
async def stream_chat(request: Request):
    # Yield a few model/dict chunks; the seam frames them as `data: <json>\n\n`
    # and auto-appends a terminal `data: [DONE]\n\n` on clean exhaustion.
    for i in range(4):
        yield Delta(index=i, text=f"tok{i}")
        await asyncio.sleep(0.01)
    yield {"index": 99, "text": "fin"}


# --- middleware stack (M17 ratelimit + auth, M18 gzip) ------------------------
# add_middleware order: LAST added is OUTERMOST. We want, from outside in:
#   GZip (compress the final bytes) -> RateLimit (shed floods) -> StaticTokenGate
#   (reject unauthenticated /v1) -> app. So add gate first, then ratelimit, then
#   gzip last. NOTE: these wrap the uvicorn ASGI seam only; the native server
#   can't host them (documented), so the native surface serves ungated.
app.add_middleware(
    StaticTokenGate,
    tokens=["e2e-secret-token"],
    scheme="Bearer",
    guard_prefixes=("/v1",),
    open_paths=("/livez", "/readyz", "/readyz-app"),
    skip_options=True,
)
app.add_middleware(
    RateLimit,
    rate=(100, 60.0),      # 100 tokens / 60s window — a per-worker safety valve
    key="ip",
    burst=100,             # generous burst so normal traffic isn't starved;
                           # the E2E flood test sends >100 to force a shed.
)
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)


if __name__ == "__main__":
    # Native Rust server surface. host/port from argv/env; ends the process.
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    app.run(host="127.0.0.1", port=port)
