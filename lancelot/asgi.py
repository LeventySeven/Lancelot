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
        if scope["type"] in ("http", "websocket"):
            p = scope.get("path", "")
            for mp, sub in getattr(app, "_mounts", ()):
                if p == mp or p.startswith(mp + "/"):
                    sub_path = p[len(mp):] or "/"
                    sub_scope = dict(scope)
                    sub_scope["path"] = sub_path
                    sub_scope["raw_path"] = sub_path.encode()
                    sub_scope.setdefault("app", _AppProxy(app))
                    await sub(sub_scope, receive, send)
                    return
        if scope["type"] == "http":
            await _http(app, scope, receive, send)
            return
        if scope["type"] == "websocket":
            await _ws(app, scope, receive, send)
            return
        raise NotImplementedError(scope["type"])

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
        # FastAPI redirect_slashes (default True): on 404, if the toggled-
        # slash path matches a route, return 307 to that path. Disabling
        # via `FastAPI(redirect_slashes=False)` keeps the 404.
        if status == 404 and getattr(app, "redirect_slashes", True):
            alt = path[:-1] if path.endswith("/") and len(path) > 1 \
                else path + "/"
            alt_route, _ = app.router.match(method, alt)
            if alt_route is not None:
                qs = scope.get("query_string", b"").decode()
                location = alt + (f"?{qs}" if qs else "")
                await send({"type": "http.response.start", "status": 307,
                            "headers": [
                                (b"location", location.encode()),
                                (b"content-length", b"0"),
                                (b"content-type",
                                 b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": b""})
                return
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
    from lancelot.pool import PoolFull, get_pool
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
    query_multi = dict(qs)  # full multi-value form (bucket-model uses)
    headers = {}  # FIRST-wins (Starlette parity; blocks header smuggling — A2)
    headers_multi: dict = {}
    for _hk, _hv in scope.get("headers", []):
        k = _hk.decode()
        headers.setdefault(k, _hv.decode())
        headers_multi.setdefault(k, []).append(_hv.decode())
    # Parse cookies from the Cookie header (RFC 6265; semicolon-separated).
    cookies: dict = {}
    if "cookie" in headers:
        from http.cookies import SimpleCookie

        try:
            sc = SimpleCookie()
            sc.load(headers["cookie"])
            cookies = {k: v.value for k, v in sc.items()}
        except Exception:  # noqa: BLE001 - malformed cookie header → empty
            cookies = {}
    params = route.match(path) or {}
    lreq = LRequest(method, path, query, headers, cookies, params, body,
                    scope["app"])
    lreq.scope_state = scope.get("state")  # honor incoming ASGI scope state
    lreq.query_params_multi = query_multi
    lreq.headers_multi = headers_multi
    from lancelot.background import BackgroundTasks as _BT

    lreq._background = _BT()  # FastAPI: fresh per request, run after response

    body_value, body_errors = None, []
    if route.body is not None:
        bname, bval = route.body
        if body:
            if getattr(route, "body_kind", "json") == "form":
                import urllib.parse as _up

                pairs = _up.parse_qsl(body.decode("latin-1"),
                                      keep_blank_values=True)
                body_value, body_errors = bval.validate_form(pairs)
            else:
                body_value, body_errors = bval.validate_json(body)
        elif getattr(route, "body_kind", "json") == "form":
            # FastAPI: empty form-body validates an empty dict against
            # the model (defaults / extra="allow" may make it succeed).
            body_value, body_errors = bval.validate_form([])
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
            # Sync handlers run on the bounded blocking pool so they never
            # pin the reactor (WBS U32 — the FastAPI cliff). Async handlers
            # are awaited directly. is_async = iscoroutinefunction(fn) is
            # decided at COMPILE time (Plan.is_async) — same basis FastAPI
            # uses — so a sync fn never runs on the loop thread. Callable
            # INSTANCE handlers (`async def __call__`) are a pre-existing
            # DI-surface gap (compile_dependant can't introspect an
            # instance) — ledgered, not silently narrowed: see
            # conformance/vendor/quarantine/test_p5_longtail.py item 4.
            # M2-P4: @app.stream — handler is an async generator yielding
            # lancelot.stream.Event; the framework owns SSE framing +
            # cancellation propagation.
            if getattr(route, "is_stream", False):
                import asyncio as _asyncio

                lreq._cancel_event = _asyncio.Event()
                gen = route.handler(**kwargs)
                await send({"type": "http.response.start", "status": 200,
                            "headers": [
                                (b"content-type", b"text/event-stream"),
                                (b"cache-control", b"no-cache"),
                                (b"x-accel-buffering", b"no")]})
                sent = True
                try:
                    async for event in gen:
                        await send({"type": "http.response.body",
                                    "body": event.render(),
                                    "more_body": True})
                except _asyncio.CancelledError:
                    lreq._cancel_event.set()
                    raise
                finally:
                    await send({"type": "http.response.body",
                                "body": b"", "more_body": False})
                return
            if route.plan.is_async:
                result = await route.handler(**kwargs)
            else:
                result = await get_pool().offload(route.handler, **kwargs)
            from lancelot.responses import Response

            if isinstance(result, Response):  # pre-serialized (docs / openapi)
                # Starlette init_headers wire order: explicit user headers
                # FIRST (lowercased — Starlette parity), then content-length,
                # then content-type LAST.
                _user_hdrs = getattr(result, "headers", None) or {}
                _hdrs = [(str(k).lower().encode(), str(v).encode())
                         for k, v in _user_hdrs.items()]
                _hdrs.append((b"content-length",
                              str(len(result.body)).encode()))
                _hdrs.append((b"content-type",
                              result.media_type.encode()))
                await send({"type": "http.response.start",
                            "status": result.status_code,
                            "headers": _hdrs})
                await send({"type": "http.response.body", "body": result.body})
                sent = True
                # BackgroundTasks run AFTER the body is sent and BEFORE
                # yield-dep teardown (still inside the async with) — FastAPI
                # ordering (Starlette runs response.background post-send).
                if lreq._background._tasks:
                    await lreq._background()
                return
            # FastAPI response_model: validate-coerce-and-serialize the
            # return value through pydantic, honouring by_alias / exclude_*
            # kwargs (default by_alias=True; the rest default False).
            if route.response_model is not None:
                from pydantic import TypeAdapter, ValidationError

                try:
                    _ta = TypeAdapter(route.response_model)
                    _val = _ta.validate_python(result)
                    payload = _ta.dump_json(
                        _val,
                        by_alias=route.response_model_by_alias,
                        exclude_unset=route.response_model_exclude_unset,
                        exclude_defaults=route.response_model_exclude_defaults,
                        exclude_none=route.response_model_exclude_none,
                        include=route.response_model_include,
                        exclude=route.response_model_exclude,
                    )
                except ValidationError:
                    # FastAPI 500: response-model coercion failure.
                    await _send_json(send, 500,
                                     b'{"detail":"Internal Server Error"}')
                    sent = True
                    return
            else:
                payload = _wire_json(result)
            # FastAPI: @app.post(..., status_code=201) sets default response
            # status when the handler returns a value (not a Response).
            default_status = route.status_code or 200
            await _send_json(send, default_status, payload)
            sent = True
            if lreq._background._tasks:  # FastAPI: post-send, pre-teardown
                await lreq._background()
    except HTTPException as exc:  # byte-faithful {"detail":…} + headers (MUST-11)
        if not sent:
            ebody = _wire_json({"detail": exc.detail})
            # Starlette JSONResponse(headers=exc.headers) wire order:
            # the exception's explicit headers FIRST (names lower-cased,
            # as Starlette init_headers does), then content-length, then
            # content-type LAST.
            hdrs = [(str(k).lower().encode(), str(v).encode())
                    for k, v in exc.headers.items()]
            hdrs.append((b"content-length", str(len(ebody)).encode()))
            hdrs.append((b"content-type", b"application/json"))
            await send({"type": "http.response.start",
                        "status": exc.status_code, "headers": hdrs})
            await send({"type": "http.response.body", "body": ebody})
    except PoolFull:  # WBS U32: overload → byte 503 + Retry-After, no hang
        if not sent:
            pbody = b'{"detail":"Service Unavailable"}'
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"retry-after", b"1"),
                                    (b"content-length",
                                     str(len(pbody)).encode()),
                                    (b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": pbody})
    except Exception:  # noqa: BLE001 - never let a handler/dep exc escape
        if not sent:
            await _send_json(send, 500, b'{"detail":"Internal Server Error"}')


async def _ws(app, scope, receive, send):
    """ASGI WebSocket seam (WBS U13). DI works like the HTTP path; a
    dependency/path validation failure → close 1008 with the JSON-encoded
    errors as reason (deep/01 line 93 / MUST-7) BEFORE accept. The Rust
    tokio-tungstenite realization is the spec §3-69 unification."""
    from lancelot.di import solve_async
    from lancelot.requests import Request as LRequest
    from lancelot.validation import _wire_json
    from lancelot.websockets import (WebSocket, WebSocketDisconnect,
                                      WebSocketException)

    path = scope["path"]
    scope.setdefault("app", _AppProxy(app))
    route, _info = app.router.match_ws(path)
    ws = WebSocket(scope, receive, send)
    if route is None:
        try:
            await receive()  # consume websocket.connect
        except Exception:  # noqa: BLE001
            return
        # RFC 6455 §7.4.1: 1006 MUST NOT be sent in a Close frame. Starlette
        # closes an unmatched WS route with 1000/"" — match it.
        await send({"type": "websocket.close", "code": 1000, "reason": ""})
        return

    params = route.match(path) or {}
    ws.path_params = params
    headers = {}
    for hk, hv in scope.get("headers", []):
        headers.setdefault(hk.decode(), hv.decode())
    lreq = LRequest("GET", path, {}, headers, {}, params, b"", scope["app"])
    lreq._ws = ws
    try:
        async with solve_async(route.plan, lreq) as res:
            if res.errors:  # MUST-7: validation failure → close 1008
                await ws.close(code=1008,
                               reason=_wire_json(res.errors).decode())
                return
            result = route.handler(**res.kwargs)
            if inspect.isawaitable(result):
                result = await result
    except WebSocketDisconnect:
        return  # client closed mid-handler — nothing to send
    except WebSocketException as exc:  # FastAPI-shape: close w/ code+reason
        try:
            await ws.close(code=exc.code, reason=exc.reason)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 - never let it escape the ASGI callable
        try:
            await ws.close(code=1011)
        except Exception:  # noqa: BLE001
            pass


async def _send_json(send, status: int, body: bytes):
    await send({
        "type": "http.response.start",
        "status": status,
        # Starlette init_headers wire order: content-length, content-type LAST.
        "headers": [(b"content-length", str(len(body)).encode()),
                    (b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body})
