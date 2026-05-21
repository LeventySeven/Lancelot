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
        """M2-P4: True after the client has disconnected mid-stream
        (set by the ASGI seam). Handlers can poll this to short-circuit
        upstream LLM calls and stop $$ on un-watched tokens."""
        ev = self._cancel_event
        return bool(ev is not None and ev.is_set())

    @property
    def state(self):
        # Honor the incoming scope["state"] (Starlette TestClient / middleware
        # seed it) over app.state — P2-deferred advisory, now implemented.
        if self.scope_state is not None:
            return self.scope_state
        return getattr(self.app, "state", None)
