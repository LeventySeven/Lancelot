"""Lancelot version of the M5 exit-gate workload.

Routes:
  POST /v1/chat/completions  → streaming passthrough to mock_upstream
  POST /api/embed            → small JSON in → ~32 KB JSON out
  POST /api/lookup           → @app.batch coalesce against rate-limited backend
  GET  /healthz              → const-tier (prebaked bytes from Rust)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import AsyncIterator

import httpx
import lancelot
import uvicorn
from lancelot.requests import Request
from lancelot.responses import StreamingResponse
from pydantic import BaseModel

UPSTREAM = os.environ.get("LC_UPSTREAM_URL", "http://127.0.0.1:8901")
EMBED_SIZE = int(os.environ.get("LC_EMBED_SIZE", "4096"))
BATCH_WINDOW_MS = int(os.environ.get("LC_BATCH_WINDOW_MS", "50"))
BATCH_MAX = int(os.environ.get("LC_BATCH_MAX", "16"))


class LookupReq(BaseModel):
    k: str = "A"
    n: int = 0


class EmbedReq(BaseModel):
    text: str = ""


# Module-level fixed embedding vector — deterministic, fast.
_EMBED_VEC = [0.5] * EMBED_SIZE


def build() -> object:
    app = lancelot.App()

    # One shared async-HTTP client for upstream calls.
    client_holder: dict = {}

    def client() -> httpx.AsyncClient:
        if "c" not in client_holder:
            client_holder["c"] = httpx.AsyncClient(timeout=60.0)
        return client_holder["c"]

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(request: Request):
        # Streaming passthrough — read body, forward upstream, stream back.
        body = await request.body()

        async def gen() -> AsyncIterator[bytes]:
            async with client().stream(
                "POST",
                f"{UPSTREAM}/v1/chat/completions",
                content=body,
                headers={"content-type": "application/json"},
            ) as resp:
                async for chunk in resp.aiter_raw():
                    yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/embed")
    async def embed(req: EmbedReq) -> dict:
        # Large-payload serialization win — vector dump dominates.
        return {"text_len": len(req.text), "embedding": _EMBED_VEC, "dim": EMBED_SIZE}

    @app.batch("/api/lookup",
               batch_window_ms=BATCH_WINDOW_MS, max_batch=BATCH_MAX,
               key=lambda it: it.k)
    async def lookup(items: list[LookupReq]) -> list[dict]:
        # ONE upstream call covers the whole batch (coalescing).
        async with client().stream("POST", f"{UPSTREAM}/v1/lookup",
                                   content=b"{}",
                                   headers={"content-type": "application/json"}) as r:
            async for _ in r.aiter_bytes():
                pass
        return [{"ok": it.n, "k": it.k} for it in items]

    @app.get("/healthz")
    async def healthz() -> str:
        return "ok"

    return app.asgi()


def main() -> None:
    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8910))
    uvicorn.run(build(), host="0.0.0.0", port=port, log_level="warning",
                access_log=False, loop="uvloop", http="httptools")


if __name__ == "__main__":
    main()
