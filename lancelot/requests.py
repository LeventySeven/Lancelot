from dataclasses import dataclass


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

    @property
    def state(self):
        # Honor the incoming scope["state"] (Starlette TestClient / middleware
        # seed it) over app.state — P2-deferred advisory, now implemented.
        if self.scope_state is not None:
            return self.scope_state
        return getattr(self.app, "state", None)
