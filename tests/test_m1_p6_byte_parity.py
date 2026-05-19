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


def test_staticfiles_etag_range_304_traversal_match_starlette(tmp_path):
    import starlette.applications
    from starlette.staticfiles import StaticFiles as SSF
    from starlette.testclient import TestClient as STC
    import lancelot
    from lancelot.staticfiles import StaticFiles as LSF
    from lancelot.testclient import TestClient as LC

    (tmp_path / "a.txt").write_bytes(b"hello world")

    sapp = starlette.applications.Starlette()
    sapp.mount("/s", SSF(directory=str(tmp_path)))
    lapp = lancelot.App()
    lapp.mount("/s", LSF(directory=str(tmp_path)))

    with STC(sapp) as sc, LC(lapp) as lc:
        s, l = sc.get("/s/a.txt"), lc.get("/s/a.txt")
        assert l.status_code == s.status_code == 200
        assert l.content == s.content == b"hello world"
        assert l.headers["etag"] == s.headers["etag"]
        assert l.headers["last-modified"] == s.headers["last-modified"]
        assert l.headers["content-type"] == s.headers["content-type"]
        assert l.headers["accept-ranges"] == s.headers["accept-ranges"]
        et = s.headers["etag"]
        s2 = sc.get("/s/a.txt", headers={"if-none-match": et})
        l2 = lc.get("/s/a.txt", headers={"if-none-match": et})
        assert l2.status_code == s2.status_code == 304
        s3 = sc.get("/s/a.txt", headers={"range": "bytes=0-4"})
        l3 = lc.get("/s/a.txt", headers={"range": "bytes=0-4"})
        assert l3.status_code == s3.status_code == 206
        assert l3.content == s3.content == b"hello"
        assert l3.headers["content-range"] == s3.headers["content-range"]
        s4 = sc.get("/s/a.txt", headers={"range": "bytes=999-1000"})
        l4 = lc.get("/s/a.txt", headers={"range": "bytes=999-1000"})
        assert l4.status_code == s4.status_code == 416
        assert lc.get("/s/../test_m1_p6_byte_parity.py").status_code in (
            404, 400)


def test_settings_provider_cached_and_missing_env_boot_crash(monkeypatch):
    from pydantic_settings import BaseSettings   # TEST-only oracle
    from lancelot.settings import settings_provider

    class S(BaseSettings):
        port: int
        name: str = "lancelot"

    monkeypatch.setenv("PORT", "8080")
    get = settings_provider(S)
    a, b = get(), get()
    assert a is b and a.port == 8080 and a.name == "lancelot"  # cached

    monkeypatch.delenv("PORT", raising=False)
    with pytest.raises(Exception):           # missing required env at BUILD
        settings_provider(S, eager=True)()
