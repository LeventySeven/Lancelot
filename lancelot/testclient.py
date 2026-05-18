import httpx

from lancelot.app import _run_lifespan_sync


class TestClient:
    """httpx client over the app's ASGI-3.0 surface. `with TestClient(app)`
    drives the app lifespan (startup on enter, shutdown on exit) — matching
    Starlette's TestClient context-manager contract."""

    def __init__(self, app, base_url: str = "http://testserver"):
        self._app = app
        self._lifespan_cm = None
        self._client = httpx.Client(
            transport=httpx.ASGITransport(app=app.asgi()),
            base_url=base_url,
        )

    def __enter__(self):
        self._lifespan_cm = _run_lifespan_sync(self._app)
        self._lifespan_cm.__enter__()
        return self

    def __exit__(self, *exc):
        if self._lifespan_cm is not None:
            self._lifespan_cm.__exit__(*exc)
        self._client.close()

    def get(self, url, **kw): return self._client.get(url, **kw)
    def post(self, url, **kw): return self._client.post(url, **kw)
    def put(self, url, **kw): return self._client.put(url, **kw)
    def delete(self, url, **kw): return self._client.delete(url, **kw)
    def patch(self, url, **kw): return self._client.patch(url, **kw)
    def request(self, *a, **kw): return self._client.request(*a, **kw)

    def websocket_connect(self, *a, **kw):  # M1-P6 (U13)
        raise NotImplementedError("WebSockets land in M1-P6")
