from dataclasses import dataclass


class _BodyAccessor(bytes):
    """A `bytes` subclass that is ALSO callable as an awaitable returning
    itself. This lets BOTH contracts coexist on `request.body`:
      - internal hot path reads it as bytes: `len(request.body)`,
        `request.body.decode()`, `validate_json(request.body)` — all work
        because it IS bytes;
      - Starlette-shape user code (custom APIRoute handlers) does
        `await request.body()` — works because instances are callable
        and return a coroutine yielding the raw bytes.
    Zero hot-path cost: it's a plain bytes value everywhere except when
    explicitly called."""

    async def __call__(self):
        return bytes(self)


@dataclass
class Request:
    method: str
    path: str
    query_params: dict
    headers: dict
    cookies: dict
    path_params: dict
    body: bytes
    app: object
    _json_cache: object = None
    scope_state: dict = None  # incoming ASGI scope["state"] (P2 advisory)
    _background: object = None  # fresh BackgroundTasks, set by the ASGI seam
    _ws: object = None  # live WebSocket, set by the ASGI websocket seam
    query_params_multi: dict = None  # full {k:[v,...]} — bucket-model needs
    headers_multi: dict = None  # full {k:[v,...]} — multi-value header bucket
    _cancel_event: object = None  # asyncio.Event set on client disconnect

    @property
    def cancelled(self) -> bool:
        """True after the client has disconnected and the ASGI seam set the
        cancel event. The event is armed on `@app.stream` routes (always) and
        on UNARY routes that opt into `cancel_on_disconnect=True` (M15 T6).
        Handlers can poll this to short-circuit upstream LLM calls and stop
        $$ on tokens nobody is watching. On routes that didn't arm a watcher
        (the fast-path default) this stays False — the seam never paid to
        poll the socket."""
        ev = self._cancel_event
        return bool(ev is not None and ev.is_set())

    async def is_disconnected(self) -> bool:
        """FastAPI-named async alias for `self.cancelled` (eases migration
        from Starlette/FastAPI `await request.is_disconnected()`). It does NOT
        itself poll receive() — the seam's background disconnect watcher (on
        stream routes / cancel_on_disconnect unary routes) sets the event; this
        just reports its state. Async so it's a drop-in for the FastAPI call
        shape, but resolves immediately."""
        return self.cancelled

    @property
    def state(self):
        # Honor the incoming scope["state"] (Starlette TestClient / middleware
        # seed it) over app.state — P2-deferred advisory, now implemented.
        # M19b: wrap the scope dict in State so BOTH request.state.x (FastAPI
        # attribute idiom) and request.state['x'] (mapping idiom) work; the
        # wrapper writes through to scope_state. (The raw dict is still on
        # request.scope_state for code that wants it.)
        if self.scope_state is not None:
            from lancelot.app import State
            return State(self.scope_state)
        return getattr(self.app, "state", None)
