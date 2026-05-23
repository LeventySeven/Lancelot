"""@app.batch peer comparison — T1.5.

Each framework gets its own /solo route that hits a SHARED-PROCESS rate-limited
mock LLM backend (asyncio.Semaphore-capped, fixed per-call latency). Lancelot
ADDITIONALLY exposes /batch via @app.batch. The comparison: what does the world
look like when you DON'T have request coalescing?

Same shape, same backend constraints across frameworks:
  upstream concurrency cap = LC_UPSTREAM_CAP (default 8)
  per-call latency         = LC_UPSTREAM_MS (default 50ms)
  batch (Lancelot only)    = window=LC_BATCH_WINDOW_MS, max=LC_MAX_BATCH

Routes (per framework):
  POST /solo   — one upstream call per request (the FastAPI-style baseline)
  GET  /stats  — counter view (call counts; Lancelot also reports coalescing
                 ratio when /batch is active)
  POST /reset  — zero the counters
Lancelot-only:
  POST /batch  — coalesces concurrent same-key items into ONE upstream call

Usage:
  python batch_peer_bench.py <framework> <port>
    framework ∈ {lancelot, fastapi, robyn, blazeio}
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from pydantic import BaseModel


# Module-scope so typing.get_type_hints (used by @app.batch's signature
# inspection) can resolve `list[Item]` annotations on nested handlers.
class Item(BaseModel):
    k: str = "A"
    n: int = 0


_UP_MS = int(os.environ.get("LC_UPSTREAM_MS", "50"))
_UP_CAP = int(os.environ.get("LC_UPSTREAM_CAP", "8"))
_WINDOW = int(os.environ.get("LC_BATCH_WINDOW_MS", "50"))
_MAXB = int(os.environ.get("LC_MAX_BATCH", "16"))
_MAX_KEYS = int(os.environ.get("LC_MAX_KEYS", "1024"))

_stats = {
    "batch_calls": 0,
    "batch_items": 0,
    "max_batch_seen": 0,
    "solo_calls": 0,
}


def _stats_view() -> dict:
    bc = _stats["batch_calls"]
    return {
        **_stats,
        "coalescing_ratio": round(_stats["batch_items"] / bc, 2) if bc else 0.0,
        "upstream_ms": _UP_MS,
        "upstream_cap": _UP_CAP,
        "window_ms": _WINDOW,
        "max_batch_cfg": _MAXB,
    }


def _reset_stats() -> None:
    for k in _stats:
        _stats[k] = 0


# ----- Lancelot (with @app.batch) ---------------------------------------------
def run_lancelot(port: int) -> None:
    import lancelot
    import uvicorn

    app = lancelot.App()
    sem_holder: dict = {}

    def upstream_sem() -> asyncio.Semaphore:
        if "s" not in sem_holder:
            sem_holder["s"] = asyncio.Semaphore(_UP_CAP)
        return sem_holder["s"]

    async def call_upstream(_n_items: int):
        async with upstream_sem():
            await asyncio.sleep(_UP_MS / 1000.0)

    @app.batch("/batch", batch_window_ms=_WINDOW, max_batch=_MAXB,
               max_keys=_MAX_KEYS, key=lambda it: it.k)
    async def batched(items: list[Item]) -> list[dict]:
        _stats["batch_calls"] += 1
        _stats["batch_items"] += len(items)
        if len(items) > _stats["max_batch_seen"]:
            _stats["max_batch_seen"] = len(items)
        await call_upstream(len(items))
        return [{"ok": it.n} for it in items]

    @app.post("/solo")
    async def solo(item: Item) -> dict:
        _stats["solo_calls"] += 1
        await call_upstream(1)
        return {"ok": item.n}

    @app.get("/stats")
    async def stats() -> dict:
        return _stats_view()

    @app.post("/reset")
    async def reset() -> dict:
        _reset_stats()
        return {"reset": True}

    uvicorn.run(app.asgi(), host="127.0.0.1", port=port, log_level="warning",
                access_log=False, loop="uvloop", http="httptools")


# ----- FastAPI (no batch) -----------------------------------------------------
def run_fastapi(port: int) -> None:
    from fastapi import FastAPI
    import uvicorn

    app = FastAPI()
    sem_holder: dict = {}

    def upstream_sem() -> asyncio.Semaphore:
        if "s" not in sem_holder:
            sem_holder["s"] = asyncio.Semaphore(_UP_CAP)
        return sem_holder["s"]

    async def call_upstream():
        async with upstream_sem():
            await asyncio.sleep(_UP_MS / 1000.0)

    @app.post("/solo")
    async def solo(item: Item) -> dict:
        _stats["solo_calls"] += 1
        await call_upstream()
        return {"ok": item.n}

    @app.get("/stats")
    async def stats() -> dict:
        return _stats_view()

    @app.post("/reset")
    async def reset() -> dict:
        _reset_stats()
        return {"reset": True}

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning",
                access_log=False, loop="uvloop", http="httptools")


# ----- Robyn (no batch) -------------------------------------------------------
def run_robyn(port: int) -> None:
    from robyn import Robyn, jsonify

    app = Robyn(__file__)
    sem_holder: dict = {}

    def upstream_sem() -> asyncio.Semaphore:
        if "s" not in sem_holder:
            sem_holder["s"] = asyncio.Semaphore(_UP_CAP)
        return sem_holder["s"]

    async def call_upstream():
        async with upstream_sem():
            await asyncio.sleep(_UP_MS / 1000.0)

    @app.post("/solo")
    async def solo(request):
        _stats["solo_calls"] += 1
        await call_upstream()
        # Robyn's request.json() returns dict; we don't strictly need to read
        # the body for this bench. Skip parsing for speed.
        return jsonify({"ok": 0})

    @app.get("/stats")
    async def stats(request):
        return jsonify(_stats_view())

    @app.post("/reset")
    async def reset(request):
        _reset_stats()
        return jsonify({"reset": True})

    app.start(host="127.0.0.1", port=port, _check_port=False)


# ----- Blazeio (no batch) -----------------------------------------------------
def run_blazeio(port: int) -> None:
    # macOS compat — same as servers.py
    import socket as _sock
    if not hasattr(_sock, "TCP_KEEPIDLE"):
        _sock.TCP_KEEPIDLE = _sock.TCP_KEEPALIVE  # type: ignore[attr-defined]

    import Blazeio as io

    web = io.App("127.0.0.1", port, with_keepalive=1)
    sem_holder: dict = {}

    def upstream_sem() -> asyncio.Semaphore:
        if "s" not in sem_holder:
            sem_holder["s"] = asyncio.Semaphore(_UP_CAP)
        return sem_holder["s"]

    async def call_upstream():
        async with upstream_sem():
            await asyncio.sleep(_UP_MS / 1000.0)

    async def solo(r):
        _stats["solo_calls"] += 1
        await call_upstream()
        body = b'{"ok": 0}'
        await r.prepare(
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(body))},
            status=200,
        )
        await r.writer(body)

    async def stats(r):
        body = json.dumps(_stats_view()).encode()
        await r.prepare(
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(body))},
            status=200,
        )
        await r.writer(body)

    async def reset(r):
        _reset_stats()
        body = b'{"reset": true}'
        await r.prepare(
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(body))},
            status=200,
        )
        await r.writer(body)

    async def before_middleware(r):
        pass

    web.add_route(solo, route_name="/solo")
    web.add_route(stats, route_name="/stats")
    web.add_route(reset, route_name="/reset")
    web.add_route(before_middleware, route_name="before_middleware")

    with web:
        web.runner()


_VARIANTS = {
    "lancelot": run_lancelot,
    "fastapi": run_fastapi,
    "robyn": run_robyn,
    "blazeio": run_blazeio,
}

if __name__ == "__main__":
    framework, port = sys.argv[1], int(sys.argv[2])
    _VARIANTS[framework](port)
