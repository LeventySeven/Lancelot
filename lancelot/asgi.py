"""Spec-compliant ASGI 3.0 callable — the migration-OUT seam and the
conformance/test driver (deliberately NOT the fast path; spec §2,
harness-design §1.2). http branch added in M1-P1 Task 5."""


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


async def _http(app, scope, receive, send):  # filled in M1-P1 Task 5
    raise NotImplementedError
