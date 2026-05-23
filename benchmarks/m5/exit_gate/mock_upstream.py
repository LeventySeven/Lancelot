"""Mock LLM/vector backend used by the M5 exit-gate apps.

Two endpoints:
  POST /v1/chat/completions  → SSE stream of N tokens at 50 tok/s with 10ms TTFT
  POST /v1/lookup           → rate-limited (asyncio.Semaphore cap=8, 50ms/call)

Stand-alone aiohttp-free implementation (raw asyncio) so deps stay tiny.

Usage:
  python mock_upstream.py <port>
Env knobs:
  MOCK_TTFT_MS   = 10        (time from request to first chunk)
  MOCK_TOK_PER_S = 50        (tokens per second after TTFT)
  MOCK_TOKENS    = 200       (tokens per chat-completions stream)
  MOCK_LOOKUP_CAP = 8        (concurrent /v1/lookup callers)
  MOCK_LOOKUP_MS = 50        (per-call latency for /v1/lookup)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

TTFT_MS = int(os.environ.get("MOCK_TTFT_MS", "10"))
TOK_PER_S = int(os.environ.get("MOCK_TOK_PER_S", "50"))
TOKENS = int(os.environ.get("MOCK_TOKENS", "200"))
LOOKUP_CAP = int(os.environ.get("MOCK_LOOKUP_CAP", "8"))
LOOKUP_MS = int(os.environ.get("MOCK_LOOKUP_MS", "50"))

_lookup_sem: asyncio.Semaphore | None = None


def get_lookup_sem() -> asyncio.Semaphore:
    global _lookup_sem
    if _lookup_sem is None:
        _lookup_sem = asyncio.Semaphore(LOOKUP_CAP)
    return _lookup_sem


async def read_head(reader: asyncio.StreamReader) -> tuple[str, str, dict, int]:
    """Parse minimal HTTP/1.1 head; returns (method, path, headers, content_length)."""
    buf = bytearray()
    while True:
        b = await reader.read(4096)
        if not b:
            raise ConnectionResetError
        buf.extend(b)
        if b"\r\n\r\n" in buf:
            break
        if len(buf) > 64 * 1024:
            raise RuntimeError("head too large")
    sep = buf.find(b"\r\n\r\n")
    head_bytes = bytes(buf[:sep])
    body_prefix = bytes(buf[sep + 4:])
    lines = head_bytes.split(b"\r\n")
    method, path, _ver = lines[0].decode().split(" ", 2)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        k, _, v = line.decode().partition(":")
        headers[k.strip().lower()] = v.strip()
    cl = int(headers.get("content-length", "0"))
    # Read the rest of the body if any was promised.
    body = bytearray(body_prefix)
    while len(body) < cl:
        chunk = await reader.read(min(cl - len(body), 65536))
        if not chunk:
            break
        body.extend(chunk)
    # Reattach body via a small wrapper on the reader — we'll just stash on
    # the request dict instead. Return the body bytes via a closure pattern:
    headers["__body"] = body.decode("utf-8", errors="replace")
    return method, path, headers, cl


def http_chunk(b: bytes) -> bytes:
    return ("%X\r\n" % len(b)).encode() + b + b"\r\n"


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        method, path, headers, _cl = await read_head(reader)
    except Exception:
        try:
            writer.close()
        except Exception:
            pass
        return

    try:
        if method == "POST" and path == "/v1/chat/completions":
            # SSE-like stream of N tokens with TTFT + steady cadence.
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: keep-alive\r\n\r\n"
            )
            await writer.drain()
            # TTFT delay.
            if TTFT_MS > 0:
                await asyncio.sleep(TTFT_MS / 1000.0)
            interval = 1.0 / max(TOK_PER_S, 1)
            for i in range(TOKENS):
                chunk = ('data: {"token":"t%d"}\n\n' % i).encode()
                writer.write(http_chunk(chunk))
                try:
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    return
                if i < TOKENS - 1 and interval > 0:
                    await asyncio.sleep(interval)
            writer.write(http_chunk(b"data: [DONE]\n\n"))
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        elif method == "POST" and path == "/v1/lookup":
            # Rate-limited; same backend cap=8, latency=50ms.
            async with get_lookup_sem():
                await asyncio.sleep(LOOKUP_MS / 1000.0)
            body = b'{"ok":1}'
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: %d\r\n"
                b"Connection: keep-alive\r\n\r\n%s" % (len(body), body)
            )
            await writer.drain()
        elif method == "GET" and path == "/healthz":
            body = b"ok"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: keep-alive\r\n\r\nok"
            )
            await writer.drain()
        else:
            body = b"not found"
            writer.write(
                b"HTTP/1.1 404 Not Found\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: %d\r\n"
                b"Connection: close\r\n\r\n%s" % (len(body), body)
            )
            await writer.drain()
    except Exception as e:
        try:
            err = f"upstream error: {e!r}".encode()
            writer.write(
                b"HTTP/1.1 500 Internal Server Error\r\n"
                b"Content-Length: %d\r\n\r\n%s" % (len(err), err)
            )
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def main(port: int) -> None:
    server = await asyncio.start_server(handle, "127.0.0.1", port,
                                        reuse_address=True)
    sock = server.sockets[0].getsockname() if server.sockets else (None, port)
    print(f"mock_upstream listening on {sock[0]}:{sock[1]}  "
          f"ttft={TTFT_MS}ms  tok_per_s={TOK_PER_S}  tokens={TOKENS}  "
          f"lookup_cap={LOOKUP_CAP}  lookup_ms={LOOKUP_MS}",
          flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
    try:
        asyncio.run(main(port))
    except KeyboardInterrupt:
        pass
