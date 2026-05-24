"""Benchmark server variants.

Two ways this module is used (see run_load.sh):
  * single-process:  python servers.py <variant> <port>
  * multi-worker:    uvicorn servers:<factory> --factory --app-dir benchmarks
                     --workers N  ... (the *_factory callables below — Lancelot
                     ASGI / Starlette / FastAPI only; Robyn and Blazeio are NOT
                     ASGI and have their own runtimes, run via servers.py
                     <robyn|blazeio> <port>)

ALL variants expose the SAME minimal endpoints:
  * GET /echo   → JSON _PAYLOAD (size tuned via LC_BENCH_ITEMS)
  * GET /stream → LC_BENCH_CHUNKS chunks of b"data: token-N\\n\\n" (SSE-shape)
The point is to isolate transport+runtime+serialization, NOT feature sets —
see benchmarks/README.md for the honest caveats.

Fairness knobs honored here:
  * uvicorn runs its PRODUCTION stack — uvloop + httptools auto-detected when
    installed (we install uvicorn[standard]); no access log; warning level.
  * the Rust server's tokio worker width is controlled by the LANCELOT_WORKERS
    env var (set by run_load.sh), so we can match thread/process counts.
  * Robyn uses its own actix-web Rust runtime; worker count via
    ROBYN_WORKERS (mirrors LANCELOT_WORKERS semantics).
  * Blazeio runs a single asyncio loop per process; scale via multiprocess
    (same `SO_REUSEPORT` model as Lancelot — Linux-only LB).

Bench fairness caveats specific to peers (M5 — documented in
benchmarks/README.md):
  * Robyn /stream returns SSEResponse → its Rust layer adds SSE framing on
    top of our raw _chunk bytes; the wire bytes are slightly different from
    Starlette/FastAPI/Lancelot's raw chunks. We measure wire RPS + TTFT, not
    bytes-exact equivalence.
  * Blazeio writes status line + headers + body MANUALLY (its design). This
    skips Starlette/FastAPI's middleware stack overhead. Headers chosen to
    match other variants byte-for-byte where possible.
"""
import json
import os
import sys

# Payload sizing for the crossover sweep (benchmarks/characterize.sh):
#   LC_BENCH_ITEMS=N  → /echo returns a list of N items (~46 B/item) instead of
#   the tiny default. Same dict for every variant; each serializes with its own
#   default (Rust pydantic-core to_json vs Starlette json.dumps).
_ITEMS = int(os.environ.get("LC_BENCH_ITEMS", "0"))
if _ITEMS > 0:
    _PAYLOAD = {"items": [{"id": i, "name": "item-%d" % i, "val": i * 3.14}
                          for i in range(_ITEMS)]}
else:
    _PAYLOAD = {"message": "ok", "n": 42}

# Streaming chunk count for the /stream endpoint (AI token-stream shape):
# LC_BENCH_CHUNKS chunks of a small fixed payload. Identical generator body
# across variants → fair transport-pump comparison.
_CHUNKS = int(os.environ.get("LC_BENCH_CHUNKS", "256"))


def _chunk(i: int) -> bytes:
    return b"data: token-%d\n\n" % i


# ---- importable app factories (used by `uvicorn ... --factory --workers N`) --
def starlette_factory():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    async def echo(request):
        return JSONResponse(_PAYLOAD)

    async def _gen():
        for i in range(_CHUNKS):
            yield _chunk(i)

    async def stream(request):
        return StreamingResponse(_gen())

    return Starlette(routes=[Route("/echo", echo), Route("/stream", stream)])


def fastapi_factory():
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    app = FastAPI()

    @app.get("/echo")
    async def echo():
        return _PAYLOAD

    @app.get("/stream")
    async def stream():
        async def gen():
            for i in range(_CHUNKS):
                yield _chunk(i)

        return StreamingResponse(gen())

    return app


def lancelot_factory():
    import lancelot

    app = lancelot.App()

    @app.get("/echo")
    async def echo():
        return _PAYLOAD

    # sse_keepalive=0 disables the per-stream keepalive task — the bench
    # path emits chunks faster than any timeout would ever fire anyway,
    # and skipping the lock acquisition per chunk recovers the last bit
    # of throughput vs Starlette (M6-T1.1 follow-up).
    @app.stream("/stream", sse_keepalive=0)
    async def stream():
        for i in range(_CHUNKS):
            yield _chunk(i)

    return app.asgi()


# ---- single-process entrypoints --------------------------------------------
def run_rust(port: int) -> None:
    # Lancelot's Rust server (hyper+tokio+pyo3). Worker width comes from
    # LANCELOT_WORKERS in the environment (set before this process starts, so
    # the runtime — built at module import — sees it).
    import lancelot._native as _native

    async def echo(req):
        return _PAYLOAD

    async def stream(req):  # R3 pump: async-gen → bounded mpsc → StreamBody
        for i in range(_CHUNKS):
            yield _chunk(i)

    _native.run([("GET", "/echo", echo), ("GET", "/stream", stream)],
                host="127.0.0.1", port=port)


def _run_uvicorn(factory, port: int) -> None:
    import uvicorn

    # Force uvicorn's PRODUCTION stack explicitly (uvloop + httptools, both
    # installed via uvicorn[standard]) so the comparison isn't against its
    # slow pure-Python h11 + asyncio fallback.
    uvicorn.run(factory(), host="127.0.0.1", port=port, log_level="warning",
                access_log=False, loop="uvloop", http="httptools")


def run_robyn(port: int) -> None:
    """Robyn 0.84.0 — pyo3 + actix-web. Has its own runtime (not ASGI).

    Fairness notes:
      * Robyn workers are configured via ROBYN_WORKERS env (defaults to 1).
      * /stream uses SSEResponse — Robyn's only first-class streaming surface.
        SSEResponse will add SSE framing (event:/data: lines) on top of our
        yielded chunk bytes. We measure RPS + TTFT, not byte-exact body parity.
    """
    from robyn import Robyn, SSEMessage, SSEResponse, jsonify

    app = Robyn(__file__)

    @app.get("/echo")
    async def echo(request):
        # jsonify returns a Response with content_type=application/json
        return jsonify(_PAYLOAD)

    @app.get("/stream")
    async def stream(request):
        async def gen():
            for i in range(_CHUNKS):
                # SSEMessage adds "data: …\n\n"; we strip our own framing to
                # avoid double-wrapping. Bench measures chunk count + TTFT.
                yield SSEMessage("token-%d" % i, id=str(i))
        return SSEResponse(gen())

    workers = int(os.environ.get("ROBYN_WORKERS", "1"))
    app.start(host="127.0.0.1", port=port, _check_port=False)
    _ = workers  # Robyn 0.84 picks workers from CLI flag; env-pinning is best-effort


def run_blazeio(port: int) -> None:
    """Blazeio 4.1.0 — pure-Python asyncio server with bespoke HTTP framing.

    Fairness notes:
      * Blazeio writes status + headers + body manually via r.writer(). We
        send the same byte body as Starlette/FastAPI (json.dumps(_PAYLOAD))
        + a minimal header set matching the others (Content-Type + Length).
      * Routing is via @web.add_route(path, method) decorator (FP style).
      * Run via app.runner() which calls asyncio.run() and blocks (matches
        the other variants' blocking pattern).
    """
    # macOS compat: Blazeio imports TCP_KEEPIDLE which is Linux-only;
    # on macOS the equivalent setsockopt name is TCP_KEEPALIVE. Alias so the
    # import succeeds — Phase B/C Linux runs use the real constant.
    import socket as _sock
    if not hasattr(_sock, "TCP_KEEPIDLE"):
        _sock.TCP_KEEPIDLE = _sock.TCP_KEEPALIVE  # type: ignore[attr-defined]

    import Blazeio as io

    web = io.App("127.0.0.1", port, with_keepalive=1)
    payload_bytes = json.dumps(_PAYLOAD).encode("utf-8")

    async def echo(r):
        # Use Blazeio's prepare() so its internal keepalive state machine sees
        # the response correctly. Manual r.writer() with HTTP/1.1 head bypasses
        # that state machine and breaks at c≥4 (0% success on oha c=50).
        await r.prepare(
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload_bytes)),
            },
            status=200,
        )
        await r.writer(payload_bytes)

    async def stream(r):
        # Chunked transfer-encoding for parity with the other streaming paths;
        # encode_resp=False so Blazeio doesn't double-frame the chunks.
        await r.prepare(
            headers={
                "Content-Type": "text/event-stream",
                "Transfer-Encoding": "chunked",
                "Cache-Control": "no-cache",
            },
            status=200,
            encode_resp=False,
        )
        for i in range(_CHUNKS):
            data = _chunk(i)
            await r.writer(b"%X\r\n%s\r\n" % (len(data), data))
        await r.writer(b"0\r\n\r\n")

    # Blazeio uses funcname_normalizers ("_" → "/"); pass route_name explicitly
    # so we don't depend on convention-driven naming.
    web.add_route(echo, route_name="/echo")
    web.add_route(stream, route_name="/stream")

    # NOTE: Blazeio's serve_route_no_middleware does `declared_routes.get(r)`
    # (passes the Request object, not r.path) — exact-match by r.path only
    # works on the with_middleware code path. Register a no-op before_middleware
    # to force that path so /echo and /stream actually match.
    async def before_middleware(r):
        pass
    web.add_route(before_middleware, route_name="before_middleware")

    # Blazeio's blocking run idiom: enter the server context, then runner().
    with web:
        web.runner()


_VARIANTS = {
    "rust": run_rust,
    "uvicorn-starlette": lambda p: _run_uvicorn(starlette_factory, p),
    "uvicorn-fastapi": lambda p: _run_uvicorn(fastapi_factory, p),
    "uvicorn-lancelot": lambda p: _run_uvicorn(lancelot_factory, p),
    "robyn": run_robyn,
    "blazeio": run_blazeio,
}

if __name__ == "__main__":
    variant, port = sys.argv[1], int(sys.argv[2])
    _VARIANTS[variant](port)
