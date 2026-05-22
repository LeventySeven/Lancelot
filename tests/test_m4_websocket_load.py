"""M4 sweep — WebSocket under REAL load + disconnect (the path most relevant to
real-time AI: voice/agent/streaming-chat sockets). Only in-process byte-parity
tests existed; this drives a real uvicorn WS server with a `websockets` client:
concurrent connections must be isolated (no cross-talk), and a client disconnect
must surface `WebSocketDisconnect` cleanly without wedging the server.
"""
import asyncio
import signal
import socket
import subprocess
import sys
import time

import pytest

websockets = pytest.importorskip("websockets")

_SERVER = r"""
import sys
import uvicorn
import lancelot
from lancelot.websockets import WebSocket, WebSocketDisconnect

app = lancelot.App()

@app.websocket("/echo")
async def echo(ws: WebSocket):    # magic-inject is by TYPE (FastAPI parity)
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            await ws.send_text("echo:" + msg)
    except WebSocketDisconnect:
        pass

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", ws="websockets")
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def server():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", _SERVER, str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 12
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(proc.stderr.read().decode()[:500])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_ws_concurrent_connections_are_isolated(server):
    # 20 concurrent sockets, each sends its own id over several round-trips —
    # every reply must be THIS socket's echo (no cross-connection bleed).
    async def one(i):
        async with websockets.connect(f"{server}/echo") as ws:
            out = []
            for r in range(3):
                await ws.send(f"c{i}-r{r}")
                out.append(await ws.recv())
            return out

    async def run():
        return await asyncio.gather(*(one(i) for i in range(20)))

    results = asyncio.run(run())
    for i, out in enumerate(results):
        assert out == [f"echo:c{i}-r{r}" for r in range(3)], (i, out)


def test_ws_client_disconnect_is_clean_and_server_survives(server):
    async def run():
        # Normal exchange, then drop the socket (context exit) → the handler
        # must see WebSocketDisconnect and not crash the server.
        async with websockets.connect(f"{server}/echo") as ws:
            await ws.send("hi")
            assert await ws.recv() == "echo:hi"
        # Abrupt close mid-conversation.
        ws2 = await websockets.connect(f"{server}/echo")
        await ws2.send("mid")
        assert await ws2.recv() == "echo:mid"
        await ws2.close()
        # Server still serves new connections.
        async with websockets.connect(f"{server}/echo") as ws3:
            await ws3.send("again")
            assert await ws3.recv() == "echo:again"

    asyncio.run(run())
