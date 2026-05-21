"""Benchmark server variants.

Two ways this module is used (see run_load.sh):
  * single-process:  python servers.py <variant> <port>
  * multi-worker:    uvicorn servers:<factory> --factory --app-dir benchmarks
                     --workers N  ... (the *_factory callables below)

ALL variants expose the SAME minimal endpoint: GET /echo → the same small JSON
object. The point is to isolate transport+runtime+serialization, NOT feature
sets — see benchmarks/README.md for the honest caveats (Rust path does no
DI/validation; FastAPI does full DI) and the threading disclosure.

Fairness knobs honored here:
  * uvicorn runs its PRODUCTION stack — uvloop + httptools auto-detected when
    installed (we install uvicorn[standard]); no access log; warning level.
  * the Rust server's tokio worker width is controlled by the LANCELOT_WORKERS
    env var (set by run_load.sh), so we can match thread/process counts.
"""
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

    @app.stream("/stream")
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


_VARIANTS = {
    "rust": run_rust,
    "uvicorn-starlette": lambda p: _run_uvicorn(starlette_factory, p),
    "uvicorn-fastapi": lambda p: _run_uvicorn(fastapi_factory, p),
    "uvicorn-lancelot": lambda p: _run_uvicorn(lancelot_factory, p),
}

if __name__ == "__main__":
    variant, port = sys.argv[1], int(sys.argv[2])
    _VARIANTS[variant](port)
