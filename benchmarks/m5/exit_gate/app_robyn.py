"""Robyn version of the M5 exit-gate workload.

No batching — /api/lookup is 1:1. Streaming via Robyn's SSEResponse.
"""
from __future__ import annotations

import json
import os
import sys

import httpx
from robyn import Robyn, SSEMessage, SSEResponse, jsonify

UPSTREAM = os.environ.get("LC_UPSTREAM_URL", "http://127.0.0.1:8901")
EMBED_SIZE = int(os.environ.get("LC_EMBED_SIZE", "4096"))

_EMBED_VEC = [0.5] * EMBED_SIZE
_client_holder: dict = {}


def client() -> httpx.AsyncClient:
    if "c" not in _client_holder:
        _client_holder["c"] = httpx.AsyncClient(timeout=60.0)
    return _client_holder["c"]


def build() -> Robyn:
    app = Robyn(__file__)

    @app.post("/v1/chat/completions")
    async def chat(request):
        # Robyn's streaming surface is SSEResponse — accepts an async generator
        # yielding SSEMessage. We bridge the upstream raw chunks back into
        # SSEMessage tokens; the framing is mostly compatible at the SSE layer.
        async def gen():
            async with client().stream(
                "POST",
                f"{UPSTREAM}/v1/chat/completions",
                content=request.body if hasattr(request, "body") else b"{}",
                headers={"content-type": "application/json"},
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        yield SSEMessage(line[6:])
        return SSEResponse(gen())

    @app.post("/api/embed")
    async def embed(request):
        # Robyn doesn't pass parsed Pydantic; we don't strictly need it.
        return jsonify({"text_len": 0, "embedding": _EMBED_VEC, "dim": EMBED_SIZE})

    @app.post("/api/lookup")
    async def lookup(request):
        async with client().stream("POST", f"{UPSTREAM}/v1/lookup",
                                   content=b"{}",
                                   headers={"content-type": "application/json"}) as r:
            async for _ in r.aiter_bytes():
                pass
        return jsonify({"ok": 0, "k": "A"})

    @app.get("/healthz")
    async def healthz(request):
        return "ok"

    return app


def main() -> None:
    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8912))
    build().start(host="0.0.0.0", port=port, _check_port=False)


if __name__ == "__main__":
    main()
