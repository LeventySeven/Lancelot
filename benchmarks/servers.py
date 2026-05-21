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
import sys

_PAYLOAD = {"message": "ok", "n": 42}


# ---- importable app factories (used by `uvicorn ... --factory --workers N`) --
def starlette_factory():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def echo(request):
        return JSONResponse(_PAYLOAD)

    return Starlette(routes=[Route("/echo", echo)])


def fastapi_factory():
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/echo")
    async def echo():
        return _PAYLOAD

    return app


def lancelot_factory():
    import lancelot

    app = lancelot.App()

    @app.get("/echo")
    async def echo():
        return _PAYLOAD

    return app.asgi()


# ---- single-process entrypoints --------------------------------------------
def run_rust(port: int) -> None:
    # Lancelot's Rust server (hyper+tokio+pyo3). Worker width comes from
    # LANCELOT_WORKERS in the environment (set before this process starts, so
    # the runtime — built at module import — sees it).
    import lancelot._native as _native

    async def echo(req):
        return _PAYLOAD

    _native.run([("GET", "/echo", echo)], host="127.0.0.1", port=port)


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
