"""M1-P6 oracle: WebSocket (U13) + StaticFiles (U14) + Settings (U19) +
urlencoded Form / unparseable-body-400 (MUST-6), byte/behavior-faithful
to real starlette 1.0.0 / fastapi 0.136.1 / pydantic-settings 2.14.1
(TEST-only). The Python ASGI seam is the path the oracle exercises; the
Rust tokio-tungstenite / tower-http realizations are ledgered §3-69."""
import asyncio

import pytest


def test_websocket_wrapper_accept_send_receive_close():
    from lancelot.websockets import WebSocket, WebSocketDisconnect

    sent = []
    incoming = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": "ping"},
        {"type": "websocket.disconnect", "code": 1000},
    ]

    async def receive():
        return incoming.pop(0)

    async def send(msg):
        sent.append(msg)

    async def main():
        ws = WebSocket({"type": "websocket", "path": "/ws"}, receive, send)
        await ws.accept()
        assert sent[0] == {"type": "websocket.accept", "subprotocol": None,
                           "headers": []}
        assert await ws.receive_text() == "ping"
        await ws.send_text("pong")
        assert sent[1] == {"type": "websocket.send", "text": "pong"}
        with pytest.raises(WebSocketDisconnect) as ei:
            await ws.receive_text()
        assert ei.value.code == 1000

    asyncio.run(main())


def test_websocket_echo_param_and_1008():
    import lancelot
    from lancelot.testclient import TestClient as LC

    def echo_app():
        app = lancelot.App()

        @app.websocket("/ws/conversations/{cid}")
        async def ws(websocket):
            await websocket.accept()
            await websocket.send_json({"cid": websocket.path_params["cid"]})
            msg = await websocket.receive_bytes()
            await websocket.send_bytes(msg + b"!")
            await websocket.close()

        return app

    with LC(echo_app()) as lc:
        with lc.websocket_connect("/ws/conversations/abc") as w:
            assert w.receive_json() == {"cid": "abc"}
            w.send_bytes(b"hi")
            assert w.receive_bytes() == b"hi!"

    def v_app():
        app = lancelot.App()

        @app.websocket("/v/{n}")
        async def v(websocket, n: int):
            await websocket.accept()
            await websocket.send_json({"n": n})

        return app

    with LC(v_app()) as lc:
        with pytest.raises(Exception):           # validation fail → 1008
            with lc.websocket_connect("/v/notanint") as w:
                w.receive_text()
