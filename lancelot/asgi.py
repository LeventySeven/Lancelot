"""Spec-compliant ASGI 3.0 callable — the migration-OUT seam and the
conformance/test driver (deliberately NOT the fast path; spec §2,
harness-design §1.2)."""
import inspect
import json as _json

from lancelot import _native


def build_asgi(app):
    async def asgi(scope, receive, send):
        if scope["type"] == "lifespan":
            await _lifespan(app, receive, send)
            return
        if scope["type"] == "http":
            await _http(app, scope, receive, send)
            return
        raise NotImplementedError(scope["type"])  # websocket -> M1-P6

    return asgi


async def _lifespan(app, receive, send):
    if app._lifespan is None:
        # Still honor the protocol (Starlette TestClient drives it).
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    cm = app._lifespan(app)
    while True:
        msg = await receive()
        if msg["type"] == "lifespan.startup":
            try:
                await cm.__aenter__()
            except BaseException as e:  # noqa: BLE001 - report per ASGI spec
                await send({"type": "lifespan.startup.failed", "message": repr(e)})
                return
            await send({"type": "lifespan.startup.complete"})
        elif msg["type"] == "lifespan.shutdown":
            try:
                await cm.__aexit__(None, None, None)
            except BaseException as e:  # noqa: BLE001
                await send({"type": "lifespan.shutdown.failed", "message": repr(e)})
                return
            await send({"type": "lifespan.shutdown.complete"})
            return


class _AppProxy:
    """Injected as scope['app'] / request.app. Harness-design §1.3: handlers &
    deps read request.app.state / .dependency_overrides / .router / .openapi()."""

    def __init__(self, app):
        self._app = app
        self.state = app.state
        self.dependency_overrides = app.dependency_overrides
        self.router = app.router

    def openapi(self):
        return self._app.openapi()


async def _http(app, scope, receive, send):
    method = scope["method"]
    path = scope["path"]

    # Honor incoming scope['state'] (Starlette TestClient seeds it); expose app.
    scope.setdefault("app", _AppProxy(app))

    route, info = app.router.match(method, path)
    if route is None:
        status = info  # 404 or 405
        body = (
            b'{"detail":"Not Found"}'
            if status == 404
            else b'{"detail":"Method Not Allowed"}'
        )
        await _send_json(send, status, body)
        return

    # Drain the request body (ASGI more_body streaming).
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return
        if not msg.get("more_body", False):
            break

    params = route.match(path) or {}
    # M1-P1: no DI/validation yet (P2). Bind path params the handler declares.
    sig = inspect.signature(route.handler)
    kwargs = {k: v for k, v in params.items() if k in sig.parameters}
    result = route.handler(**kwargs)
    if inspect.isawaitable(result):
        result = await result

    # Serialize via the SAME pydantic-core seam as the Rust path (no drift).
    try:
        out = bytes(_native._to_json(result))
    except Exception:  # noqa: BLE001 - fallback keeps the spine robust
        out = _json.dumps(result).encode()
    await _send_json(send, 200, out)


async def _send_json(send, status: int, body: bytes):
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body})
