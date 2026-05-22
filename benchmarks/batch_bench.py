"""@app.batch (C5 marquee) effectiveness benchmark — server.

Honest question: does prefix-aware request grouping actually COALESCE concurrent
same-key requests into far fewer upstream calls, and does that win on throughput
when the upstream is the bottleneck? Correctness is already covered by
tests/test_m2_tier_c5_batch.py; this measures effectiveness under load.

Model: a simulated upstream LLM with a shared CONCURRENCY CAP (the realistic
constraint — providers rate-limit concurrent calls and charge per call). BOTH
the batched route and the per-request "solo" route hit the SAME capped upstream,
so the only difference is grouping. Run servers.py-style via uvicorn.

Routes (same workload, same upstream):
  POST /batch  — @app.batch, coalesces same-key items into one handler call
  POST /solo   — one upstream call per request (the FastAPI-style baseline)
  GET  /stats  — upstream_calls / items / max_batch_seen (the coalescing proof)
  POST /reset  — zero the counters between runs

Env knobs: LC_UPSTREAM_MS (per-call latency, default 50),
           LC_UPSTREAM_CAP (max concurrent upstream calls, default 8),
           LC_BATCH_WINDOW_MS (default 50), LC_MAX_BATCH (default 16).
"""
import asyncio
import os
import sys

import lancelot
from pydantic import BaseModel

_UP_MS = int(os.environ.get("LC_UPSTREAM_MS", "50"))
_UP_CAP = int(os.environ.get("LC_UPSTREAM_CAP", "8"))
_WINDOW = int(os.environ.get("LC_BATCH_WINDOW_MS", "50"))
_MAXB = int(os.environ.get("LC_MAX_BATCH", "16"))
_MAX_KEYS = int(os.environ.get("LC_MAX_KEYS", "1024"))

_stats = {"batch_calls": 0, "batch_items": 0, "max_batch_seen": 0, "solo_calls": 0}


class Item(BaseModel):
    k: str = "A"
    n: int = 0


def build():
    app = lancelot.App()
    # Shared capped upstream — created lazily on the running loop.
    sem: dict = {}

    def upstream_sem() -> asyncio.Semaphore:
        if "s" not in sem:
            sem["s"] = asyncio.Semaphore(_UP_CAP)
        return sem["s"]

    async def call_upstream(n_items: int):
        # One "LLM call" covering n_items, gated by the shared concurrency cap.
        async with upstream_sem():
            await asyncio.sleep(_UP_MS / 1000.0)

    @app.batch("/batch", batch_window_ms=_WINDOW, max_batch=_MAXB,
               max_keys=_MAX_KEYS, key=lambda it: it.k)
    async def batched(items: list[Item]) -> list[dict]:
        _stats["batch_calls"] += 1
        _stats["batch_items"] += len(items)
        _stats["max_batch_seen"] = max(_stats["max_batch_seen"], len(items))
        await call_upstream(len(items))  # ONE upstream call for the whole batch
        return [{"ok": it.n} for it in items]

    @app.post("/solo")
    async def solo(item: Item) -> dict:
        _stats["solo_calls"] += 1
        await call_upstream(1)  # one upstream call per request
        return {"ok": item.n}

    @app.get("/stats")
    async def stats() -> dict:
        ratio = (_stats["batch_items"] / _stats["batch_calls"]
                 if _stats["batch_calls"] else 0.0)
        return {**_stats, "coalescing_ratio": round(ratio, 2),
                "upstream_ms": _UP_MS, "upstream_cap": _UP_CAP,
                "window_ms": _WINDOW, "max_batch_cfg": _MAXB}

    @app.post("/reset")
    async def reset() -> dict:
        for k in _stats:
            _stats[k] = 0
        return {"reset": True}

    return app.asgi()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build(), host="127.0.0.1", port=int(sys.argv[1]),
                log_level="warning", access_log=False,
                loop="uvloop", http="httptools")
