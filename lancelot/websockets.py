"""ASGI WebSocket wrapper — Starlette-shape surface (accept/receive_*/
send_*/close + WebSocketDisconnect). WebSockets are served via the
Python ASGI seam under uvicorn (this module); there is NO native Rust
WebSocket path today (the M19b round-2 recheck flagged a stale docstring
that referenced a nonexistent src/ws/mod.rs — a native tokio-tungstenite
path would be a deliberate future addition, not a shipped feature)."""
import json


class _WSDMeta(type):
    """Metaclass for WebSocketDisconnect that ALSO matches Starlette's
    WebSocketDisconnect via duck-typing on isinstance(). The conformance
    swap runs through Starlette's TestClient, which raises its own
    `starlette.websockets.WebSocketDisconnect`; the vendored tests catch
    via `from fastapi.websockets import WebSocketDisconnect` (= ours).
    A single class hierarchy bridges this without importing Starlette at
    `lancelot.websockets` load time (cardinal rule: pydantic-only runtime).
    """

    def __instancecheck__(cls, instance):
        if type.__instancecheck__(cls, instance):
            return True
        # Cross-stack match — only when Starlette IS already loaded
        # elsewhere (we do NOT import it ourselves).
        import sys as _sys

        sm = _sys.modules.get("starlette.websockets")
        if sm is not None:
            sl = getattr(sm, "WebSocketDisconnect", None)
            if sl is not None and isinstance(instance, sl):
                return True
        return False


class WebSocketDisconnect(Exception, metaclass=_WSDMeta):
    def __init__(self, code: int = 1000, reason: str = ""):
        self.code = code
        self.reason = reason
        super().__init__(f"disconnect {code}")


class WebSocketException(Exception):
    """Starlette/FastAPI-shape: `raise WebSocketException(code=1008,
    reason=...)` propagates out of a handler; the asgi seam closes the
    WS with the given code/reason (HTTPException analog for WebSockets)."""

    def __init__(self, code: int, reason: str | None = None):
        self.code = code
        self.reason = reason or ""
        super().__init__(f"WebSocketException(code={code})")


class WebSocket:
    def __init__(self, scope, receive, send):
        self.scope = scope
        self._receive = receive
        self._send = send
        self.path_params = {}
        self._accepted = False

    async def accept(self, subprotocol=None, headers=None):
        msg = await self._receive()  # consume websocket.connect
        if msg["type"] != "websocket.connect":
            raise RuntimeError(
                f"expected websocket.connect, got {msg['type']}")
        await self._send({"type": "websocket.accept",
                          "subprotocol": subprotocol,
                          "headers": list(headers or [])})
        self._accepted = True

    async def _recv(self):
        msg = await self._receive()
        if msg["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(msg.get("code", 1000),
                                      msg.get("reason", ""))
        return msg

    async def receive_text(self) -> str:
        return (await self._recv())["text"]

    async def receive_bytes(self) -> bytes:
        return (await self._recv())["bytes"]

    async def receive_json(self):
        return json.loads(await self.receive_text())

    async def send_text(self, data: str):
        await self._send({"type": "websocket.send", "text": data})

    async def send_bytes(self, data: bytes):
        await self._send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data):
        await self.send_text(json.dumps(data, separators=(",", ":")))

    async def close(self, code: int = 1000, reason: str = ""):
        await self._send({"type": "websocket.close", "code": code,
                          "reason": reason})
