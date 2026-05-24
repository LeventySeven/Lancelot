"""M7 T4.9 — Response.set_retry_hint() emits X-Should-Retry header.

Turbopuffer pattern: server-controlled retry overrides client SDK's
status-code defaults. Lets a handler tell clients "this 500 is permanent,
don't retry" vs "this 4xx is transient, retry me."
"""
import lancelot
from lancelot.responses import Response, JSONResponse
from lancelot import HTTPException
from lancelot.testclient import TestClient


def test_set_retry_hint_true_emits_header():
    app = lancelot.App()

    @app.get("/transient-error", response_model=None)
    async def transient_error() -> Response:
        r = JSONResponse({"error": "transient"}, status_code=409)
        r.set_retry_hint(True)
        return r

    with TestClient(app) as client:
        resp = client.get("/transient-error")
        assert resp.status_code == 409
        assert resp.headers.get("x-should-retry") == "true"


def test_set_retry_hint_false_emits_header():
    app = lancelot.App()

    @app.get("/permanent-error", response_model=None)
    async def permanent_error() -> Response:
        r = JSONResponse({"error": "permanent"}, status_code=500)
        r.set_retry_hint(False)
        return r

    with TestClient(app) as client:
        resp = client.get("/permanent-error")
        assert resp.status_code == 500
        assert resp.headers.get("x-should-retry") == "false"


def test_set_retry_hint_absent_when_not_called():
    app = lancelot.App()

    @app.get("/ok", response_model=None)
    async def ok() -> Response:
        return JSONResponse({"ok": True}, status_code=200)

    with TestClient(app) as client:
        resp = client.get("/ok")
        # No retry hint when handler doesn't call set_retry_hint.
        assert "x-should-retry" not in resp.headers
