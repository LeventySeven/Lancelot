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
    async def _run_on_event(kind):
        # FastAPI on_event decorator handlers — startup runs all at
        # startup-complete time; shutdown at shutdown-complete time.
        # Awaitable returns are awaited; sync results are ignored.
        import inspect as _isp

        for fn in app._on_event_handlers.get(kind, []):
            r = fn()
            if _isp.isawaitable(r):
                await r

    if app._lifespan is None:
        # Still honor the protocol (Starlette TestClient drives it).
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await _run_on_event("startup")
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await _run_on_event("shutdown")
                # Same rationale as above: do not auto-flip on lifespan.
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
            # NOTE: we do NOT auto-flip `_shutting_down` here. A TestClient
            # context-exit also fires lifespan.shutdown, and if we flipped,
            # subsequent tests reusing the same module-level `app` would
            # see /readyz 503 + drain-refusals. The flag is a *graceful
            # shutdown* signal — users opt-in via `app.initiate_shutdown()`
            # or `app.install_signal_handlers()`. The lifespan hook just
            # invokes user shutdown code.
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

    # Tier-B.B1: refuse NEW requests while draining. /readyz and /livez
    # are still served (k8s needs them during shutdown). Sending 503 +
    # Retry-After tells the LB to back off (RFC 9110 §15.6.4).
    if app._shutting_down and path not in ("/livez", "/readyz"):
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"retry-after", b"1"),
                                (b"content-length", b"23"),
                                (b"content-type", b"application/json")]})
        await send({"type": "http.response.body",
                    "body": b'{"detail":"draining"}'})
        return
    # Tier-B.B4: global concurrent-request cap → 503 + Retry-After before
    # we acquire the slot (no half-counted state). None = unlimited (FastAPI
    # default). Health probes bypass so an LB can still poll under load.
    if app._max_concurrent_requests is not None \
            and app._active_requests >= app._max_concurrent_requests \
            and path not in ("/livez", "/readyz"):
        # FIX: the body was `b'...overloaded"}'[:28]` but that string is 30
        # bytes — the slice cut the closing `"}`, emitting INVALID JSON
        # (`{"detail":"server overloaded`) with content-length 28. Use the full
        # body + its real length.
        _ov = b'{"detail":"server overloaded"}'
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"retry-after", b"1"),
                                (b"content-length", str(len(_ov)).encode()),
                                (b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": _ov})
        return

    # Tier-B.B3 request-id + W3C trace context, captured in ONE header scan
    # (was two full scans — efficiency). request-id: from X-Request-ID /
    # X-Correlation-ID if present (echoed back ONLY then — FastAPI byte-parity),
    # else a fresh internal UUID4. traceparent: a valid W3C header exposes its
    # trace-id via context.trace_id and is forwarded by TransportPool onto
    # outbound LLM calls (the AI request joins the same distributed trace).
    from lancelot.context import (request_id as _rid_var, new_request_id,
                                  trace_id as _tid_var, trace_parent as _tp_var,
                                  parse_traceparent)

    _rid = None
    _rid_from_client = False
    _tp_raw = None
    for _hk, _hv in scope.get("headers", []):
        if _rid is None and _hk in (b"x-request-id", b"x-correlation-id"):
            # SECURITY: we echo this back — strip CR/LF/NUL (response-header
            # smuggling) and bound the length so a tiny client can't cost a
            # huge header.
            _cleaned = (_hv.decode("ascii", "ignore").replace("\r", "")
                        .replace("\n", "").replace("\x00", ""))[:128]
            if _cleaned:
                _rid = _cleaned
                _rid_from_client = True
        elif _tp_raw is None and _hk == b"traceparent":
            _tp_raw = _hv.decode("ascii", "ignore")
    if not _rid:
        _rid = new_request_id()
    _tid = parse_traceparent(_tp_raw) if _tp_raw else None

    # Tier-B.B1+B4+B2: in-flight counter + RED in-flight gauge + duration clock.
    import time as _time
    from lancelot.metrics import in_flight_delta

    _t0 = _time.perf_counter()
    scope["_lancelot_t0"] = _t0  # request start → streaming TTFT (time-to-first-chunk)
    _final_status_holder = [500]  # mutable so we can set it from below
    _rid_token = _tid_token = _tp_token = None

    async def send_with_rid(msg):
        # Echo `x-request-id` ONLY when the client sent one (FastAPI byte-parity
        # for the common no-correlation case). Also capture the final status.
        # M7 T2.1: emit `x-lancelot-cost-usd` ALWAYS when cost was accumulated
        # (the handler chose to set it; zero by default → no header).
        if msg.get("type") == "http.response.start":
            _final_status_holder[0] = msg.get("status", 500)
            hdrs = msg.get("headers")
            need_mutation = False
            if _rid_from_client and not any(
                    k == b"x-request-id" for k, _ in (hdrs or [])):
                need_mutation = True
            try:
                from lancelot.context import cost_usd as _cost_var
                _cost_now = _cost_var.get()
                if _cost_now > 0.0 and not any(
                        k == b"x-lancelot-cost-usd" for k, _ in (hdrs or [])):
                    need_mutation = True
            except Exception:  # noqa: BLE001
                _cost_now = 0.0
            if need_mutation:
                hdrs = list(hdrs or [])
                if _rid_from_client and not any(
                        k == b"x-request-id" for k, _ in hdrs):
                    hdrs.append((b"x-request-id",
                                 _rid.encode("ascii", "ignore")))
                if _cost_now > 0.0 and not any(
                        k == b"x-lancelot-cost-usd" for k, _ in hdrs):
                    hdrs.append((b"x-lancelot-cost-usd",
                                 f"{_cost_now:.6f}".encode("ascii")))
                msg = {**msg, "headers": hdrs}
        await send(msg)

    # Set the ContextVars + bump the counters INSIDE the try so the `finally`
    # ALWAYS unwinds them — no leak window if anything between entry and
    # dispatch raises (audit HIGH-1). These mutations are the only ones; the
    # 503 cap check above already read `_active_requests` pre-increment.
    try:
        app._active_requests += 1
        in_flight_delta(1)
        _rid_token = _rid_var.set(_rid)
        if _tid:
            _tid_token = _tid_var.set(_tid)
            _tp_token = _tp_var.set(_tp_raw.strip().lower())
        await _http_dispatch(app, scope, receive, send_with_rid)
    finally:
        app._active_requests -= 1
        in_flight_delta(-1)
        try:
            from lancelot.metrics import record_request
            # M7 T2.1 + T2.4: pass tenant + cost into the metric record.
            # Both default to "-"/0.0 if the handler didn't set them; no
            # cardinality / cost-metric inflation in the common case.
            try:
                from lancelot.context import (
                    cost_usd as _cost_var,
                    tenant_id as _tenant_var,
                )
                _cost = _cost_var.get()
                _tenant = _tenant_var.get()
            except Exception:  # noqa: BLE001
                _cost, _tenant = 0.0, "-"

            record_request(path, method, _final_status_holder[0],
                           _time.perf_counter() - _t0,
                           tenant=_tenant, cost_usd=_cost)
        except Exception:  # noqa: BLE001
            pass
        try:
            if _rid_token is not None:
                _rid_var.reset(_rid_token)
            if _tid_token is not None:
                _tid_var.reset(_tid_token)
            if _tp_token is not None:
                _tp_var.reset(_tp_token)
        except Exception:  # noqa: BLE001
            pass


def _stream_chunk_bytes(ev):
    """Coerce one `@app.stream` yield to wire bytes. Accepts `bytes`/`str`
    (raw passthrough — like Starlette's StreamingResponse, the shape AI
    handlers naturally yield: `yield f"data: {token}\n\n"`) AND
    `lancelot.stream.Event` (SSE-framed via `.render()`); anything else is a
    JSON line. FIX: the loop previously called `event.render()` unconditionally,
    so a `bytes`/`str` yield raised after the 200 was sent → SILENT EMPTY body."""
    if isinstance(ev, (bytes, bytearray)):
        return bytes(ev)
    if isinstance(ev, str):
        return ev.encode("utf-8")
    render = getattr(ev, "render", None)
    if callable(render):
        out = render()
        return out if isinstance(out, (bytes, bytearray)) else str(out).encode("utf-8")
    import json as _json
    return (_json.dumps(ev, separators=(",", ":"), default=str) + "\n").encode("utf-8")


async def _http_dispatch(app, scope, receive, send):
    """The original `_http` body, factored out so the wrapper above can
    own the per-request lifecycle (counter, request-id, RED metrics)
    without indenting 470 lines of existing code."""
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
                # FastAPI/Starlette emit ABSOLUTE Location URLs on
                # redirect_slashes (scheme + host[:port] + path + ?qs).
                # Build from the ASGI scope so reverse-proxy headers /
                # base_url overrides are honored.
                _scheme = scope.get("scheme", "http")
                _host = ""
                for _hk, _hv in scope.get("headers", []):
                    if _hk == b"host":
                        try:
                            _host = _hv.decode("ascii", "ignore")
                        except Exception:  # noqa: BLE001
                            _host = ""
                        break
                if not _host:
                    server = scope.get("server")
                    if isinstance(server, (list, tuple)) and len(server) >= 1:
                        _host = str(server[0])
                        if len(server) >= 2 and server[1] not in (None, 80, 443):
                            _host = f"{_host}:{server[1]}"
                location = f"{_scheme}://{_host}{alt}" if _host else alt
                if qs:
                    location += f"?{qs}"
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
    # A custom ASGI middleware MAY raise HTTPException from within receive()
    # (e.g. a content-size limiter); catch it here so the canonical
    # {"detail":exc.detail} 4xx emits — instead of bubbling out as an
    # unhandled server exception.
    from lancelot.exceptions import HTTPException as _MWHE
    from lancelot.validation import _wire_json as _mw_wire

    body = b""
    try:
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            if len(body) > _MAX_BODY:  # A4: bound the migration-path body
                await _send_json(send, 413,
                                 b'{"detail":"Request entity too large"}')
                return
            if not msg.get("more_body", False):
                break
    except _MWHE as _mw_exc:
        ebody = _mw_wire({"detail": _mw_exc.detail})
        hdrs = [(str(k).lower().encode(), str(v).encode())
                for k, v in (_mw_exc.headers or {}).items()]
        hdrs.append((b"content-length", str(len(ebody)).encode()))
        hdrs.append((b"content-type", b"application/json"))
        await send({"type": "http.response.start",
                    "status": _mw_exc.status_code, "headers": hdrs})
        await send({"type": "http.response.body", "body": ebody})
        return

    # A3 (research-distillation): JSON-nesting-depth DoS guard. Runs ONLY
    # for JSON-ish content types so multipart / urlencoded / binary bodies
    # are untouched. 128 is Robyn's value; raise via App config when we
    # expose the knob.
    if body:
        _ct = b""
        for _hk, _hv in scope.get("headers", []):
            if _hk == b"content-type":
                _ct = _hv
                break
        if b"json" in _ct.lower():
            from lancelot._json_guard import exceeds_depth

            if exceeds_depth(body, 128):
                await _send_json(send, 400,
                                 b'{"detail":"JSON body nested too deep"}')
                return
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
    # We intentionally do NOT use http.cookies.SimpleCookie — it silently
    # DROPS cookies whose values contain RFC-6265-illegal-but-real-world-
    # common characters like `[`, `]`, `"`, `,`, breaking e.g. JSON-shaped
    # cookies. FastAPI/Starlette also bypass SimpleCookie for the same
    # reason. Our parser: split on `;`, strip whitespace, first `=` is the
    # name/value boundary, RFC-6265 §4.1.1 quoted values are de-quoted.
    cookies: dict = {}
    if "cookie" in headers:
        for _piece in headers["cookie"].split(";"):
            _piece = _piece.strip()
            if not _piece:
                continue
            _i = _piece.find("=")
            if _i <= 0:
                continue  # malformed pair (no `=` or starts with `=`)
            _ck = _piece[:_i].strip()
            _cv = _piece[_i + 1:].strip()
            # RFC 6265 §4.1.1: cookie-value MAY be quoted-string.
            if len(_cv) >= 2 and _cv[0] == '"' and _cv[-1] == '"':
                _cv = _cv[1:-1]
            # First occurrence wins (Starlette parity; defends against
            # cookie-bomb where a client re-asserts a known name).
            cookies.setdefault(_ck, _cv)
    params = route.match(path) or {}
    from lancelot.requests import _BodyAccessor as _BA

    lreq = LRequest(method, path, query, headers, cookies, params,
                    _BA(body), scope["app"])
    lreq.scope_state = scope.get("state")  # honor incoming ASGI scope state
    lreq.query_params_multi = query_multi
    lreq.headers_multi = headers_multi
    from lancelot.background import BackgroundTasks as _BT

    lreq._background = _BT()  # FastAPI: fresh per request, run after response

    body_value, body_errors = None, []
    if route.body is not None:
        # FastAPI Content-Type rules for JSON-body routes:
        #   strict=True:  any non-JSON CT (incl. missing) → 422
        #   strict=False: missing CT is OK; an EXPLICIT non-JSON CT → 422
        # Only enforced when a body is actually present (empty body falls
        # through to the canonical missing-field 422).
        if body and getattr(route, "body_kind", "json") == "json":
            _ct = ""
            for _hk, _hv in scope.get("headers", []):
                if _hk == b"content-type":
                    try:
                        _ct = _hv.decode("latin-1").lower()
                    except Exception:  # noqa: BLE001
                        _ct = ""
                    break
            _strict = getattr(route, "strict_content_type", False)
            # Reject when strict and CT is non-JSON (incl. missing), OR
            # when lax and CT is present but explicitly non-JSON.
            _ct_invalid = (
                "json" not in _ct
                and (_strict or (_ct and "json" not in _ct))
            )
            if _ct_invalid:
                from lancelot.validation import _wire_json

                err_body = _wire_json({"detail": [{
                    "type": "content_type",
                    "loc": ["body"],
                    "msg": "Content-Type must be application/json",
                    "input": None,
                }]})
                await _send_json(send, 422, err_body)
                sent = True
                return
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
            # FastAPI: an empty body for a model route returns
            # loc=["body"] (root-level missing) OR loc=["body", alias]
            # depending on the field — let pydantic generate the
            # alias-aware error via an empty-object validate.
            body_value, body_errors = bval.validate_json(b"{}")
            if not body_errors:
                # All fields optional → still 422 with root-level loc
                # (FastAPI: missing-body uses just ["body"]).
                body_errors = [{"type": "missing", "loc": ["body"],
                                "msg": "Field required", "input": None}]

    # Handler call + response MUST be inside the solve_async context so
    # yield-dependency teardown runs AFTER the response (PEP-789, deep/01 §5).
    # A handler/dep exception propagates through solve_async (which feeds the
    # real exc_info into the yield-dep AsyncExitStack so `except:`/`finally:`
    # rollback runs — B4) and is mapped to 500 here so it never escapes the
    # ASGI callable (A5).
    sent = False
    # Function-scope yield-deps register on this stack so they tear down
    # BETWEEN handler-return and response-emit (FastAPI scope="function").
    # Request-scope deps stay on the inner solve_async stack and tear
    # down AFTER the response body (B4 semantics — M1-P2).
    import contextlib as _cl_func

    _func_stack = _cl_func.AsyncExitStack()
    await _func_stack.__aenter__()
    _func_stack_closed = False

    async def _close_func_scope():
        nonlocal _func_stack_closed
        if not _func_stack_closed:
            _func_stack_closed = True
            await _func_stack.aclose()

    # FastAPI custom-route extension: if the route's class OVERRODE
    # get_route_handler (i.e. it's not the base Route's), dispatch via
    # that opt-in path instead of the fast inline path. The wrapper
    # callable owns body-parse + solve + serialize and returns a
    # Response we then emit. The common-case hot path below is untouched.
    from lancelot.routing import Route as _BaseRoute
    if type(route).get_route_handler is not _BaseRoute.get_route_handler:
        try:
            _custom = route.get_route_handler()
            _resp = await _custom(lreq)
            await _emit_response_object(_resp, scope, receive, send, lreq)
            sent = True
            if lreq._background._tasks:
                await lreq._background()
            return
        except HTTPException as _che:
            # The custom handler may `raise HTTPException(...)` (e.g.
            # mapping a RequestValidationError). Emit it byte-faithfully.
            h = _find_exc_handler(app, _che)
            if h is not None:
                await _emit_handler_response(h, lreq, _che, scope,
                                             receive, send)
            else:
                ebody = _wire_json({"detail": _che.detail})
                hdrs = [(str(k).lower().encode(), str(v).encode())
                        for k, v in (_che.headers or {}).items()]
                hdrs.append((b"content-length", str(len(ebody)).encode()))
                hdrs.append((b"content-type", b"application/json"))
                await send({"type": "http.response.start",
                            "status": _che.status_code, "headers": hdrs})
                await send({"type": "http.response.body", "body": ebody})
            sent = True
            return
        finally:
            await _close_func_scope()

    try:
        async with solve_async(route.plan, lreq,
                               _func_stack=_func_stack) as res:
            errs = list(res.errors) + body_errors
            if errs:
                # FastAPI-parity: if the user registered a handler for
                # RequestValidationError, synthesize one and dispatch.
                # Otherwise emit the default byte-faithful 422.
                from lancelot.exceptions import RequestValidationError as _RVE

                h = _find_exc_handler(app, _RVE(errs))
                if h is not None:
                    rve = _RVE(errs)
                    await _emit_handler_response(
                        h, lreq, rve, scope, receive, send)
                    sent = True
                    return
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

                # C2 (validated under load — M4): poll receive() for
                # http.disconnect WHILE the handler runs, so an in-flight
                # TransportPool call bound to this request is cancelled the
                # moment the client leaves. Without this, nothing watched the
                # disconnect during the handler await, so it was only noticed
                # when a send failed — i.e. AFTER the upstream had already
                # completed (the user paid for tokens nobody was watching).
                async def _watch_disconnect():
                    # Catch BaseException (incl. CancelledError AND any server-
                    # adapter receive() error): stop watching CLEANLY, never
                    # leave an unretrieved task exception (audit MED-1). The task
                    # then completes normally so the `finally` cancel is a no-op.
                    try:
                        while True:
                            m = await receive()
                            if m.get("type") == "http.disconnect":
                                lreq._cancel_event.set()
                                return
                    except BaseException:  # noqa: BLE001
                        return

                _disc_watch = _asyncio.ensure_future(_watch_disconnect())
                gen = route.handler(**kwargs)
                await send({"type": "http.response.start", "status": 200,
                            "headers": [
                                (b"content-type", b"text/event-stream"),
                                (b"cache-control", b"no-cache"),
                                (b"x-accel-buffering", b"no")]})
                sent = True
                # A5 (research-distillation): 15-second SSE keepalive ping
                # on idle, so CDNs / nginx / browsers don't tear the
                # connection down during slow LLM streams. The ping is an
                # SSE comment line (":\n\n") — clients are required to
                # ignore comments (RFC 8895 / WHATWG EventSource §9).
                # A6: stream_interval — batch N events per SSE frame for
                # high-rate token streams. Default 1 (unbuffered).
                import time as _time  # for TTFT (time-to-first-chunk)

                _ka = getattr(route, "sse_keepalive", 15.0)
                _interval = max(1, int(getattr(route, "stream_interval", 1)))
                _buf = bytearray()
                _buf_count = 0
                _ttft_done = False

                # (M6-T1.1: original `_flush` removed; the
                # `_flush_serialized` below replaces it and serializes
                # sends through the keepalive lock — see fix note.)

                _it = gen.__aiter__()
                # M6-T1.1 (fixes M5 lab-body 10× regression on
                # uvicorn-lancelot /stream): the previous loop wrapped
                # every __anext__ in wait_for(shield(ensure_future(...))),
                # so each chunk paid for 3 task allocations + a timeout
                # handle just to enforce the SSE keepalive idle deadline.
                # At c=50 × 256 chunks/req that's ~64k extra asyncio ops/s
                # — the native rust R3 pump skips all of this (one
                # _drain coroutine + sink.try_push per chunk).
                #
                # Fix: keepalive lives in a single background task that
                # only fires when the main loop has been idle for _ka
                # seconds. Sends are serialized through _send_locked
                # (asyncio.Lock); the main loop is a clean async-for.
                _last_send = _time.monotonic()
                _send_lock = _asyncio.Lock() \
                    if (_ka is not None and _ka > 0) else None

                async def _send_serialized(msg):
                    nonlocal _last_send
                    if _send_lock is not None:
                        async with _send_lock:
                            await send(msg)
                            _last_send = _time.monotonic()
                    else:
                        await send(msg)
                        _last_send = _time.monotonic()

                async def _flush_serialized():
                    nonlocal _buf, _buf_count, _ttft_done
                    if _buf:
                        if not _ttft_done:
                            _ttft_done = True
                            try:
                                from lancelot.metrics import record_stream_ttft
                                _st = scope.get("_lancelot_t0")
                                if _st is not None:
                                    record_stream_ttft(
                                        path, method,
                                        _time.perf_counter() - _st)
                            except Exception:  # noqa: BLE001
                                pass
                        await _send_serialized({
                            "type": "http.response.body",
                            "body": bytes(_buf), "more_body": True})
                        _buf = bytearray()
                        _buf_count = 0

                async def _keepalive_loop():
                    # Wake up periodically; only emit a ping if idle has
                    # exceeded _ka. Sleeping for half the window keeps
                    # max-idle-before-ping at ≤1.5 × _ka in the worst case.
                    try:
                        while True:
                            await _asyncio.sleep(_ka / 2.0)
                            idle = _time.monotonic() - _last_send
                            if idle >= _ka:
                                # Flush any buffered chunks first so a
                                # slow client doesn't lose data behind
                                # the ping.
                                await _flush_serialized()
                                await _send_serialized({
                                    "type": "http.response.body",
                                    "body": b":\n\n", "more_body": True})
                    except _asyncio.CancelledError:
                        return

                _kp_task = (_asyncio.ensure_future(_keepalive_loop())
                            if _send_lock is not None else None)
                try:
                    async for event in _it:
                        _buf += _stream_chunk_bytes(event)
                        _buf_count += 1
                        if _buf_count >= _interval:
                            await _flush_serialized()
                except _asyncio.CancelledError:
                    lreq._cancel_event.set()
                    raise
                finally:
                    if _kp_task is not None and not _kp_task.done():
                        _kp_task.cancel()
                    if not _disc_watch.done():
                        _disc_watch.cancel()
                    await _flush_serialized()
                    await _send_serialized({
                        "type": "http.response.body",
                        "body": b"", "more_body": False})
                return
            # FastAPI: an async-generator handler (async def with yield)
            # returns the async-gen directly; calling it (no await) gives
            # the iterator to stream.
            import inspect as _isp_local
            # Tier-B.B5: per-route timeout. asyncio.wait_for cancels the
            # awaitable on timeout, which (via the surrounding
            # solve_async async-with) feeds CancelledError into every
            # active yield-dep's __exit__ — so DB rollback / file-close
            # / etc. STILL runs (B4 semantics preserved). Streaming /
            # async-gen handlers are not wrapped (their per-chunk
            # cancellation already happens via request.cancelled).
            import asyncio as _asyncio_timeout
            _to = route.timeout

            if _isp_local.isasyncgenfunction(route.handler) or \
                    _isp_local.isgeneratorfunction(route.handler):
                result = route.handler(**kwargs)
            elif route.plan.is_async:
                if _to is not None:
                    try:
                        result = await _asyncio_timeout.wait_for(
                            route.handler(**kwargs), timeout=_to)
                    except _asyncio_timeout.TimeoutError:
                        await _send_json(send, 504,
                                         b'{"detail":"Request timeout"}')
                        sent = True
                        return
                else:
                    result = await route.handler(**kwargs)
            else:
                if _to is not None:
                    try:
                        result = await _asyncio_timeout.wait_for(
                            get_pool().offload(route.handler, **kwargs),
                            timeout=_to)
                    except _asyncio_timeout.TimeoutError:
                        await _send_json(send, 504,
                                         b'{"detail":"Request timeout"}')
                        sent = True
                        return
                else:
                    result = await get_pool().offload(
                        route.handler, **kwargs)
            # Tear down scope="function" yield-deps NOW (between handler
            # return and response emit) — FastAPI parity. Request-scope
            # deps stay on the inner solve_async stack and close after
            # the response body finishes.
            await _close_func_scope()
            from lancelot.responses import Response

            # FastAPI: a handler returning a raw async-iter / generator
            # gets auto-wrapped in a StreamingResponse (JSON lines for
            # iterators of dicts). Detect and wrap.
            import inspect as _isp
            if not isinstance(result, Response) and (
                    _isp.isasyncgen(result) or _isp.isgenerator(result)
                    or hasattr(result, "__aiter__")):
                from lancelot.responses import StreamingResponse
                import json as _json

                # FastAPI: detect `-> AsyncIterable[Model]` / `Iterable[T]`
                # on the handler and validate each yielded item against
                # the element type. ResponseValidationError on failure.
                _item_adapter = None
                try:
                    import typing as _t

                    _hints = _t.get_type_hints(route.handler,
                                               include_extras=True)
                    _ret = _hints.get("return")
                    _ret_origin = _t.get_origin(_ret) or _ret
                    # iter / async-iter / generator / async-generator types
                    _iter_origins = {
                        c for c in (
                            getattr(_t, "Iterable", None),
                            getattr(_t, "AsyncIterable", None),
                            getattr(_t, "Iterator", None),
                            getattr(_t, "AsyncIterator", None),
                            getattr(_t, "Generator", None),
                            getattr(_t, "AsyncGenerator", None))
                        if c is not None
                    }
                    # Also match collections.abc versions (origin of
                    # typing.AsyncIterable is collections.abc.AsyncIterable).
                    import collections.abc as _abc

                    _iter_origins |= {
                        _abc.Iterable, _abc.AsyncIterable, _abc.Iterator,
                        _abc.AsyncIterator, _abc.Generator,
                        _abc.AsyncGenerator}
                    if _ret_origin in _iter_origins:
                        _args = _t.get_args(_ret)
                        if _args:
                            from pydantic import TypeAdapter as _TA
                            _item_adapter = _TA(_args[0])
                except Exception:  # noqa: BLE001
                    _item_adapter = None

                async def _aiter_json(it):
                    from lancelot.exceptions import ResponseValidationError
                    from pydantic import ValidationError as _VE

                    if _isp.isasyncgen(it) or hasattr(it, "__aiter__"):
                        async for x in it:
                            if _item_adapter is not None:
                                try:
                                    x = _item_adapter.validate_python(x)
                                    yield _item_adapter.dump_json(x)
                                    continue
                                except _VE as _e:
                                    raise ResponseValidationError(
                                        _e.errors(), body=x) from _e
                            yield _json.dumps(x).encode() if not isinstance(
                                x, (bytes, str)) else (
                                x if isinstance(x, bytes) else x.encode())
                    else:
                        for x in it:
                            if _item_adapter is not None:
                                try:
                                    x = _item_adapter.validate_python(x)
                                    yield _item_adapter.dump_json(x)
                                    continue
                                except _VE as _e:
                                    raise ResponseValidationError(
                                        _e.errors(), body=x) from _e
                            yield _json.dumps(x).encode() if not isinstance(
                                x, (bytes, str)) else (
                                x if isinstance(x, bytes) else x.encode())
                result = StreamingResponse(_aiter_json(result),
                                           media_type="application/jsonl")
            # Duck-type for any Starlette-shape ASGI-callable response a
            # handler may return (FileResponse, RedirectResponse,
            # PlainTextResponse from starlette.responses, ...). Detect
            # before the strict lancelot.responses.Response isinstance so
            # FastAPI parity for `return FileResponse(...)` works.
            if not isinstance(result, Response) and callable(result) \
                    and getattr(result, "media_type", None) is not None \
                    and getattr(result, "body_iterator", None) is None \
                    and not hasattr(result, "body_iterator"):
                # Some Starlette responses (FileResponse, RedirectResponse)
                # implement __call__(scope, receive, send); delegate.
                try:
                    await result(scope, receive, send)
                    sent = True
                    return
                except TypeError:
                    pass  # not actually an ASGI app — fall through
            if isinstance(result, Response):  # pre-serialized (docs / openapi)
                _user_hdrs = getattr(result, "headers", None) or {}
                # StreamingResponse: iterate `body_iterator` (async or
                # sync) and emit chunks (no Content-Length; chunked
                # transfer). Other Response subclasses: one-shot body.
                _iterator = getattr(result, "body_iterator", None)
                if _iterator is not None:
                    _hdrs = [(str(k).lower().encode(), str(v).encode())
                             for k, v in _user_hdrs.items()]
                    _hdrs.append((b"content-type",
                                  result.media_type.encode()))
                    await send({"type": "http.response.start",
                                "status": result.status_code,
                                "headers": _hdrs})
                    # Mark `sent` BEFORE iterating: if the body iterator
                    # raises mid-stream (e.g. ResponseValidationError on
                    # per-item validation), the outer except MUST NOT try
                    # to emit a second http.response.start — Starlette's
                    # TestClient asserts that. The exception still
                    # propagates per Starlette parity (raise_server_excs).
                    sent = True
                    import inspect as _isp

                    if _isp.isasyncgen(_iterator) or hasattr(
                            _iterator, "__aiter__"):
                        async for chunk in _iterator:
                            if isinstance(chunk, str):
                                chunk = chunk.encode("utf-8")
                            await send({"type": "http.response.body",
                                        "body": chunk, "more_body": True})
                    else:
                        for chunk in _iterator:
                            if isinstance(chunk, str):
                                chunk = chunk.encode("utf-8")
                            await send({"type": "http.response.body",
                                        "body": chunk, "more_body": True})
                    await send({"type": "http.response.body",
                                "body": b"", "more_body": False})
                    if lreq._background._tasks:
                        await lreq._background()
                    return
                # Starlette init_headers wire order: explicit user headers
                # FIRST (lowercased — Starlette parity), then content-length,
                # then content-type LAST. List-valued entries (multi
                # Set-Cookie) emit one header per value.
                _hdrs = []
                for k, v in _user_hdrs.items():
                    kb = str(k).lower().encode()
                    if isinstance(v, list):
                        for vv in v:
                            _hdrs.append((kb, str(vv).encode()))
                    else:
                        _hdrs.append((kb, str(v).encode()))
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
            # For a BaseModel response_model use model_validate with
            # from_attributes=True so a different BaseModel instance is
            # coerced ORM-style (fields outside response_model are dropped).
            if route.response_model is not None:
                from pydantic import BaseModel as _BM, TypeAdapter, \
                    ValidationError

                try:
                    rm = route.response_model
                    if (isinstance(rm, type) and issubclass(rm, _BM)
                            and not isinstance(result, rm)):
                        _val = rm.model_validate(result, from_attributes=True)
                    else:
                        # `from_attributes=True` also matters for the
                        # list[Model] / dict[str, Model] / Optional[Model]
                        # paths — TypeAdapter recursively coerces ORM-style
                        # objects only when this flag is set.
                        _val = TypeAdapter(rm).validate_python(
                            result, from_attributes=True)
                    _ta = TypeAdapter(rm)
                    payload = _ta.dump_json(
                        _val,
                        by_alias=route.response_model_by_alias,
                        exclude_unset=route.response_model_exclude_unset,
                        exclude_defaults=route.response_model_exclude_defaults,
                        exclude_none=route.response_model_exclude_none,
                        include=route.response_model_include,
                        exclude=route.response_model_exclude,
                    )
                except ValidationError as _ve:
                    # FastAPI: response-model coercion failure raises
                    # ResponseValidationError (the testclient sees it; a
                    # real ASGI server logs and sends 500).
                    from lancelot.exceptions import ResponseValidationError

                    raise ResponseValidationError(
                        _ve.errors(), body=result) from _ve
            else:
                payload = _wire_json(result)
            # FastAPI: @app.post(..., status_code=201) sets default response
            # status when the handler returns a value (not a Response).
            default_status = route.status_code or 200
            # Merge the `response: Response` scratch (set_cookie / headers /
            # status_code) — FastAPI parity (deep/01 §6). Scratch.status_code
            # WINS over route.status_code if non-None.
            scratch = getattr(lreq, "_resp_scratch", None)
            extra_hdrs = None
            if scratch is not None:
                if scratch.status_code is not None:
                    default_status = scratch.status_code
                if scratch.headers:
                    extra_hdrs = []
                    for k, v in scratch.headers.items():
                        kb = str(k).lower().encode()
                        if isinstance(v, list):
                            for vv in v:
                                extra_hdrs.append((kb, str(vv).encode()))
                        else:
                            extra_hdrs.append((kb, str(v).encode()))
            # FastAPI `response_class=ORJSONResponse / PlainTextResponse /
            # HTMLResponse / ...`: instantiate it with the (already-resolved)
            # `result` (or already-serialized `payload` for JSONResponse) and
            # emit. Without response_class we fall through to the byte-fast
            # JSON path.
            if route.response_class is not None:
                # Build a response object — its render() decides bytes.
                _rcls = route.response_class
                # If the handler returned a Response we already handled it
                # above; here `result` is a Python value.
                try:
                    _r = _rcls(result)
                except TypeError:
                    # Some Response subclasses take (content, status_code).
                    _r = _rcls(result, default_status)
                # Starlette-shape ASGI-callable responses (FileResponse,
                # RedirectResponse, StreamingResponse) implement
                # `__call__(scope, receive, send)` — delegate to that so
                # file streaming / redirect headers / etc. work correctly.
                # `body` on these is None; reading it would emit an empty
                # response. Lancelot's own Response subclasses store body
                # bytes directly and we emit manually below.
                _body_attr = getattr(_r, "body", None)
                if _body_attr is None and callable(_r):
                    await _r(scope, receive, send)
                    sent = True
                else:
                    _r_status = (scratch.status_code
                                 if scratch is not None
                                 and scratch.status_code is not None
                                 else default_status)
                    _hdrs = list(extra_hdrs or [])
                    _mt = getattr(_r, "media_type", None) \
                        or getattr(_rcls, "media_type", None) \
                        or "application/json"
                    _body_bytes = _body_attr or b""
                    _hdrs.append((b"content-length",
                                  str(len(_body_bytes)).encode()))
                    _hdrs.append((b"content-type", str(_mt).encode()))
                    await send({"type": "http.response.start",
                                "status": _r_status, "headers": _hdrs})
                    await send({"type": "http.response.body",
                                "body": _body_bytes})
            else:
                await _send_json(send, default_status, payload,
                                 extra_headers=extra_hdrs)
            sent = True
            if lreq._background._tasks:  # FastAPI: post-send, pre-teardown
                await lreq._background()
    except HTTPException as exc:  # byte-faithful {"detail":…} + headers (MUST-11)
        if not sent:
            # FastAPI-parity: a user-registered handler wins over the
            # default byte-faithful 400/401/403/4xx emit. The default
            # path stays byte-identical when no handler is registered
            # (conformance gate).
            h = _find_exc_handler(app, exc)
            if h is not None:
                await _emit_handler_response(
                    h, lreq, exc, scope, receive, send)
                sent = True
                return
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
    except Exception as _exc:  # noqa: BLE001
        # FastAPI/Starlette semantics (verified against starlette
        # ServerErrorMiddleware): a user-registered Exception-handler
        # emits its Response, then the ORIGINAL exception is re-raised.
        # The harness (TestClient with raise_server_exceptions=True, or
        # the production ASGI server's own error middleware) decides what
        # to do with the re-raise — pytest.raises catches it; production
        # logs and moves on.
        if not sent:
            h = _find_exc_handler(app, _exc)
            if h is not None:
                try:
                    await _emit_handler_response(
                        h, lreq, _exc, scope, receive, send)
                    sent = True
                except Exception:  # noqa: BLE001
                    # The handler itself blew up — fall through to the
                    # default 500 emit so the operator sees the original
                    # failure (re-raised below), not a swallowed bug.
                    pass
        if not sent:
            # Starlette parity: the default 500 is PLAIN TEXT
            # "Internal Server Error" (PlainTextResponse), NOT JSON
            # `{"detail":...}`. Tests assert the literal text body.
            err = b"Internal Server Error"
            await send({"type": "http.response.start", "status": 500,
                        "headers": [
                            (b"content-length", str(len(err)).encode()),
                            (b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": err})
            sent = True
        raise
    finally:
        # Defensive: in case neither the post-handler close nor an
        # exception-path teardown ran, close function-scope deps now.
        # Idempotent (the helper guards against double-close).
        try:
            await _close_func_scope()
        except Exception:  # noqa: BLE001
            pass


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
    headers_multi: dict = {}
    for hk, hv in scope.get("headers", []):
        k = hk.decode()
        headers.setdefault(k, hv.decode())
        headers_multi.setdefault(k, []).append(hv.decode())
    # Parse query params from the WS handshake (Cookie / Query DI need them).
    from urllib.parse import parse_qs as _pqs

    qs = _pqs(scope.get("query_string", b"").decode())
    query = {k: v[-1] for k, v in qs.items()}
    # Lenient cookie parser (same as HTTP seam — SimpleCookie drops
    # cookies with brackets/quotes; ours keeps them).
    cookies: dict = {}
    if "cookie" in headers:
        for _piece in headers["cookie"].split(";"):
            _piece = _piece.strip()
            if not _piece:
                continue
            _i = _piece.find("=")
            if _i <= 0:
                continue
            _ck = _piece[:_i].strip()
            _cv = _piece[_i + 1:].strip()
            if len(_cv) >= 2 and _cv[0] == '"' and _cv[-1] == '"':
                _cv = _cv[1:-1]
            cookies.setdefault(_ck, _cv)
    lreq = LRequest("GET", path, query, headers, cookies, params, b"",
                    scope["app"])
    lreq.query_params_multi = dict(qs)
    lreq.headers_multi = headers_multi
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
        # Re-raise so Starlette's TestClient (raise_app_exceptions=True)
        # surfaces the disconnect to the user thread. FastAPI does the
        # same — the vendored tutorial002 / tutorial001 tests assert
        # `pytest.raises(WebSocketDisconnect)` on the `with` block exit.
        # In production this is wrapped by the ASGI server's WS adapter
        # which logs and closes the socket cleanly.
        raise
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


def _find_exc_handler(app, exc):
    """Walk `exc`'s MRO against app.exception_handlers (most-specific
    wins). Returns the handler callable or None.

    FastAPI-parity: a registered handler for `Exception` matches anything
    derived from Exception (used for the 500 catch-all in vendored
    test_exception_handlers.py)."""
    handlers = getattr(app, "exception_handlers", None) or {}
    if not handlers:
        return None
    for cls in type(exc).__mro__:
        h = handlers.get(cls)
        if h is not None:
            return h
    return None


async def _emit_handler_response(handler, lreq, exc, scope, receive, send):
    """Invoke a user exception handler with (request, exc) and emit its
    Response. Supports BOTH Lancelot's Response shape (read .body /
    .status_code / .headers / .media_type) AND any Starlette-shape
    Response (ASGI-callable `response(scope, receive, send)`)."""
    import inspect as _inspect

    out = handler(lreq, exc)
    if _inspect.isawaitable(out):
        out = await out
    # Starlette-shape: any ASGI-callable response (its __call__ takes
    # scope/receive/send and emits itself). FastAPI vendored tests use
    # `from starlette.responses import JSONResponse`.
    if callable(out) and not hasattr(out, "body"):
        await out(scope, receive, send)
        return
    if hasattr(out, "__call__") and hasattr(out, "body") \
            and getattr(out, "media_type", None) is not None:
        # Lancelot Response OR Starlette Response: emit directly.
        try:
            await out(scope, receive, send)
            return
        except TypeError:
            # Lancelot Response isn't an ASGI app — fall through to
            # the explicit emit below.
            pass
    # Lancelot Response (no __call__): read fields and emit manually.
    body = getattr(out, "body", b"") or b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    status_code = getattr(out, "status_code", 200) or 200
    media_type = getattr(out, "media_type", "application/json") \
        or "application/json"
    user_hdrs = getattr(out, "headers", None) or {}
    hdrs = []
    for k, v in user_hdrs.items():
        kb = str(k).lower().encode()
        if isinstance(v, list):
            for vv in v:
                hdrs.append((kb, str(vv).encode()))
        else:
            hdrs.append((kb, str(v).encode()))
    hdrs.append((b"content-length", str(len(body)).encode()))
    hdrs.append((b"content-type", str(media_type).encode()))
    await send({"type": "http.response.start", "status": status_code,
                "headers": hdrs})
    await send({"type": "http.response.body", "body": body})


async def _emit_response_object(resp, scope, receive, send, lreq):
    """Emit a Response object returned by a custom get_route_handler.
    Handles Starlette-shape ASGI-callable responses (delegate to __call__)
    AND Lancelot's Response (read body / status / headers / media_type)."""
    # Starlette-shape ASGI-callable (FileResponse / StreamingResponse /
    # plain Starlette Response) — body is None / it has __call__.
    _body = getattr(resp, "body", None)
    if _body is None and callable(resp):
        await resp(scope, receive, send)
        return
    body = _body if isinstance(_body, (bytes, bytearray)) else (
        str(_body).encode() if _body is not None else b"")
    status = getattr(resp, "status_code", 200) or 200
    media_type = getattr(resp, "media_type", None) or "application/json"
    user_hdrs = getattr(resp, "headers", None) or {}
    hdrs = []
    for k, v in user_hdrs.items():
        kb = str(k).lower().encode()
        if isinstance(v, list):
            for vv in v:
                hdrs.append((kb, str(vv).encode()))
        else:
            hdrs.append((kb, str(v).encode()))
    hdrs.append((b"content-length", str(len(body)).encode()))
    hdrs.append((b"content-type", str(media_type).encode()))
    await send({"type": "http.response.start", "status": status,
                "headers": hdrs})
    await send({"type": "http.response.body", "body": body})


async def _send_json(send, status: int, body: bytes, *, extra_headers=None):
    # Starlette init_headers wire order: explicit user headers FIRST
    # (lowercased), then content-length, then content-type LAST. Multi-value
    # entries (multi Set-Cookie) are flattened to one header per value.
    # RFC 7230 §3.3.2: 1xx/204/205/304 MUST NOT include a message body.
    # Strip body and content-related headers when the status disallows it.
    no_body = status < 200 or status in (204, 205, 304)
    hdrs = list(extra_headers or [])
    if not no_body:
        hdrs.append((b"content-length", str(len(body)).encode()))
        hdrs.append((b"content-type", b"application/json"))
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": hdrs,
    })
    await send({"type": "http.response.body",
                "body": b"" if no_body else body})
