"""FastAPI version of the M5 exit-gate workload.

No @app.batch — /api/lookup hits the rate-limited upstream 1:1
(the FastAPI-baseline shape).
"""
from __future__ import annotations

import os
import sys
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

UPSTREAM = os.environ.get("LC_UPSTREAM_URL", "http://127.0.0.1:8901")
EMBED_SIZE = int(os.environ.get("LC_EMBED_SIZE", "4096"))


class LookupReq(BaseModel):
    k: str = "A"
    n: int = 0


class EmbedReq(BaseModel):
    text: str = ""


_EMBED_VEC = [0.5] * EMBED_SIZE


def build() -> FastAPI:
    app = FastAPI()
    client_holder: dict = {}

    def client() -> httpx.AsyncClient:
        if "c" not in client_holder:
            client_holder["c"] = httpx.AsyncClient(timeout=60.0)
        return client_holder["c"]

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> StreamingResponse:
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
        return {"text_len": len(req.text), "embedding": _EMBED_VEC, "dim": EMBED_SIZE}

    @app.post("/api/lookup")
    async def lookup(req: LookupReq) -> dict:
        # 1:1 — no batching available.
        async with client().stream("POST", f"{UPSTREAM}/v1/lookup",
                                   content=b"{}",
                                   headers={"content-type": "application/json"}) as r:
            async for _ in r.aiter_bytes():
                pass
        return {"ok": req.n, "k": req.k}

    @app.get("/healthz")
    async def healthz() -> str:
        return "ok"

    return app


def main() -> None:
    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8911))
    uvicorn.run(build(), host="0.0.0.0", port=port, log_level="warning",
                access_log=False, loop="uvloop", http="httptools")


if __name__ == "__main__":
    main()
