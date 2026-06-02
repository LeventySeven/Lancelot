"""M19b — three code-verified conformance bugs caught by the round-2 founder
recheck (code-grounded verifier), all reproduced before fixing.

1. OpenAPI nested-Security drop: a Security() scheme nested inside a Depends()
   never reaches op['security'] / components['securitySchemes'] (di.py collected
   only DIRECT security; openapi.py read only top-level). Breaks fastapi-users'
   whole value + Polar's Speakeasy SDK (/docs Authorize button vanishes).
2. Duplicate STATIC same-path = last-registered-wins (routing.py const-tier dict
   overwrite) vs FastAPI first-wins — 500s LangServe's default add_routes trick.
3. request.state.X raises AttributeError: request.state returned a plain dict, so
   the FastAPI attribute idiom failed (the M19 "fixed #1 blocker" claim only
   covered dict-subscript). Must support BOTH attribute and subscript.
"""
import lancelot
from lancelot import Depends, TestClient


# --- Bug 1: OpenAPI nested-Security flattening ---
def test_nested_security_appears_in_openapi():
    from lancelot.security import OAuth2PasswordBearer

    oauth2 = OAuth2PasswordBearer(tokenUrl="token")
    app = lancelot.App()

    def current_user(token: str = Depends(oauth2)):  # scheme NESTED in a dep
        return {"t": token}

    @app.get("/me")
    def me(user=Depends(current_user)):
        return user

    with TestClient(app) as c:
        spec = c.get("/openapi.json").json()
    op = spec["paths"]["/me"]["get"]
    schemes = (spec.get("components") or {}).get("securitySchemes") or {}
    assert op.get("security"), f"op.security empty for a nested scheme: {op.get('security')}"
    assert "OAuth2PasswordBearer" in schemes, f"securitySchemes missing the scheme: {schemes}"


# --- Bug 2: duplicate static path resolves FIRST-registered-wins (FastAPI parity) ---
def test_duplicate_static_path_first_registered_wins():
    app = lancelot.App()

    @app.get("/dup")
    def first(request: lancelot.Request):
        return {"who": "first"}

    @app.get("/dup")
    def second(request: lancelot.Request):
        return {"who": "second"}

    with TestClient(app) as c:
        body = c.get("/dup").json()
    assert body == {"who": "first"}, f"expected first-registered to win (FastAPI parity), got {body}"


# --- Bug 3: request.state supports attribute AND subscript access ---
def test_request_state_attribute_and_subscript():
    from starlette.middleware.base import BaseHTTPMiddleware

    class TenantMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.tenant = "acme"  # Starlette attribute write -> scope[state]
            return await call_next(request)

    app = lancelot.App()
    app.add_middleware(TenantMW)

    @app.get("/who")
    def who(request: lancelot.Request):
        return {
            "attr": request.state.tenant,        # FastAPI idiom (was AttributeError)
            "subscript": request.state["tenant"],  # dict idiom (must still work)
            "raw": request.scope_state["tenant"],   # raw dict still available
            "in": "tenant" in request.state,        # membership
        }

    with TestClient(app) as c:
        r = c.get("/who")
    assert r.status_code == 200, r.text
    assert r.json() == {"attr": "acme", "subscript": "acme", "raw": "acme", "in": True}
