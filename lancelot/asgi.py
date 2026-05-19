"""Spec-compliant ASGI 3.0 callable — the migration-OUT seam and the
conformance/test driver (deliberately NOT the fast path; spec §2,
harness-design §1.2)."""
import inspect

_MAX_BODY = 16 * 1024 * 1024  # 16 MiB cap on the ASGI migration path (A4)


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
        self.override_plans = app._override_plans  # per-app cache (A2)
        self.router = app.router

    def openapi(self):
        return self._app.openapi()


async def _http(app, scope, receive, send):
    method = scope["method"]
    path = scope["path"]

    # Expose app as scope['app'] (harness-design §1.3). NOTE: honoring the
    # incoming scope['state'] (request.state seeding) is DEFERRED to M1-P2,
    # when Request/DI land — there is no code reading scope['state'] yet.
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

    from urllib.parse import parse_qs

    from lancelot.di import solve_async
    from lancelot.exceptions import HTTPException
    from lancelot.requests import Request as LRequest
    from lancelot.validation import errors_to_422, _wire_json

    # Drain the body (ASGI more_body streaming); capped at _MAX_BODY → 413
    # (enforced just below). The ASGI path is the migration on-ramp, not the
    # fast path; a configurable/per-route limit is a P-any item.
    body = b""
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return
        body += msg.get("body", b"")
        if len(body) > _MAX_BODY:  # A4: bound the migration-path body (DoS)
            await _send_json(send, 413, b'{"detail":"Request entity too large"}')
            return
        if not msg.get("more_body", False):
            break

    qs = parse_qs(scope.get("query_string", b"").decode())
    query = {k: v[-1] for k, v in qs.items()}
    headers = {}  # FIRST-wins (Starlette parity; blocks header smuggling — A2)
    for _hk, _hv in scope.get("headers", []):
        headers.setdefault(_hk.decode(), _hv.decode())
    params = route.match(path) or {}
    lreq = LRequest(method, path, query, headers, {}, params, body, scope["app"])
    lreq.scope_state = scope.get("state")  # honor incoming ASGI scope state

    body_value, body_errors = None, []
    if route.body is not None:
        bname, bval = route.body
        if body:
            body_value, body_errors = bval.validate_json(body)
        else:
            body_errors = [{"type": "missing", "loc": ["body", bname],
                            "msg": "Field required", "input": None}]

    # Handler call + response MUST be inside the solve_async context so
    # yield-dependency teardown runs AFTER the response (PEP-789, deep/01 §5).
    # A handler/dep exception propagates through solve_async (which feeds the
    # real exc_info into the yield-dep AsyncExitStack so `except:`/`finally:`
    # rollback runs — B4) and is mapped to 500 here so it never escapes the
    # ASGI callable (A5).
    sent = False
    try:
        async with solve_async(route.plan, lreq) as res:
            errs = list(res.errors) + body_errors
            if errs:
                status, out = errors_to_422(errs)
                await _send_json(send, status, out)
                sent = True
                return
            kwargs = dict(res.kwargs)
            if route.body is not None:
                kwargs[route.body[0]] = body_value
            result = route.handler(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            from lancelot.responses import Response

            if isinstance(result, Response):  # pre-serialized (docs / openapi)
                await send({"type": "http.response.start",
                            "status": result.status_code,
                            "headers": [
                                (b"content-type", result.media_type.encode()),
                                (b"content-length",
                                 str(len(result.body)).encode())]})
                await send({"type": "http.response.body", "body": result.body})
                sent = True
                return
            payload = _wire_json(result)
            await _send_json(send, 200, payload)
            sent = True
    except HTTPException as exc:  # byte-faithful {"detail":…} + headers (MUST-11)
        if not sent:
            ebody = _wire_json({"detail": exc.detail})
            hdrs = [(b"content-type", b"application/json"),
                    (b"content-length", str(len(ebody)).encode())]
            for k, v in exc.headers.items():
                hdrs.append((str(k).encode(), str(v).encode()))
            await send({"type": "http.response.start",
                        "status": exc.status_code, "headers": hdrs})
            await send({"type": "http.response.body", "body": ebody})
    except Exception:  # noqa: BLE001 - never let a handler/dep exc escape
        if not sent:
            await _send_json(send, 500, b'{"detail":"Internal Server Error"}')


async def _send_json(send, status: int, body: bytes):
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
