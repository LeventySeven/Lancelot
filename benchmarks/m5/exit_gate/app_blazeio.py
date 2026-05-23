"""Blazeio version of the M5 exit-gate workload.

No batching — /api/lookup is 1:1. Streaming via manual r.writer() of
chunked SSE-shaped chunks.
"""
from __future__ import annotations

import json
import os
import sys

# macOS compat — Blazeio uses Linux-only socket constant.
import socket as _sock
if not hasattr(_sock, "TCP_KEEPIDLE"):
    _sock.TCP_KEEPIDLE = _sock.TCP_KEEPALIVE  # type: ignore[attr-defined]

import httpx
import Blazeio as io

UPSTREAM = os.environ.get("LC_UPSTREAM_URL", "http://127.0.0.1:8901")
EMBED_SIZE = int(os.environ.get("LC_EMBED_SIZE", "4096"))

_EMBED_VEC = [0.5] * EMBED_SIZE
_EMBED_BODY = json.dumps(
    {"text_len": 0, "embedding": _EMBED_VEC, "dim": EMBED_SIZE}
).encode("utf-8")

_client_holder: dict = {}


def client() -> httpx.AsyncClient:
    if "c" not in _client_holder:
        _client_holder["c"] = httpx.AsyncClient(timeout=60.0)
    return _client_holder["c"]


def main() -> None:
    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8913))
    web = io.App("0.0.0.0", port, with_keepalive=1)

    async def healthz(r):
        body = b"ok"
        await r.prepare(headers={"Content-Type": "text/plain",
                                  "Content-Length": str(len(body))},
                        status=200)
        await r.writer(body)

    async def embed(r):
        await r.prepare(headers={"Content-Type": "application/json",
                                  "Content-Length": str(len(_EMBED_BODY))},
                        status=200)
        await r.writer(_EMBED_BODY)

    async def lookup(r):
        async with client().stream("POST", f"{UPSTREAM}/v1/lookup",
                                   content=b"{}",
                                   headers={"content-type": "application/json"}) as up:
            async for _ in up.aiter_bytes():
                pass
        body = b'{"ok":0,"k":"A"}'
        await r.prepare(headers={"Content-Type": "application/json",
                                  "Content-Length": str(len(body))},
                        status=200)
        await r.writer(body)

    async def chat(r):
        await r.prepare(headers={"Content-Type": "text/event-stream",
                                  "Transfer-Encoding": "chunked",
                                  "Cache-Control": "no-cache"},
                        status=200, encode_resp=False)
        async with client().stream("POST", f"{UPSTREAM}/v1/chat/completions",
                                    content=b"{}",
                                    headers={"content-type": "application/json"}) as up:
            async for chunk in up.aiter_raw():
                # Re-frame as HTTP chunked.
                await r.writer(b"%X\r\n%s\r\n" % (len(chunk), chunk))
        await r.writer(b"0\r\n\r\n")

    async def before_middleware(r):
        pass

    web.add_route(healthz, route_name="/healthz")
    web.add_route(embed, route_name="/api/embed")
    web.add_route(lookup, route_name="/api/lookup")
    web.add_route(chat, route_name="/v1/chat/completions")
    web.add_route(before_middleware, route_name="before_middleware")

    with web:
        web.runner()


if __name__ == "__main__":
    main()
