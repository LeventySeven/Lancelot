"""M20 Batch A — DX / adoption tests (A-T1.1, A-T1.2, A-T1.3, A-T2.1).

Items verified:
  A-T1.1  @app.stream routes advertise text/event-stream in build_openapi.
  A-T1.2  build_openapi forwards servers, description, openapi_tags.
  A-T1.3  OAuth2AuthorizationCodeBearer, OpenIdConnect, OAuth2PasswordRequestFormStrict
            importable from lancelot.security and produce correct OpenAPI schemes.
  A-T2.1  sync yield-dep __exit__ runs off the event loop (no deadlock under load).
"""
import asyncio
import threading

import pytest


# ---------------------------------------------------------------------------
# A-T1.1: @app.stream → text/event-stream in generated OpenAPI spec
# ---------------------------------------------------------------------------

def test_stream_route_openapi_media_type():
    """A stream route must advertise text/event-stream, not application/json."""
    from lancelot.app import App
    from lancelot.stream import Event

    app = App()

    @app.stream("/tokens")
    async def token_stream(prompt: str):
        yield Event(data="hello")
        yield Event(data="world")

    spec = app.openapi()
    resp = spec["paths"]["/tokens"]["get"]["responses"]["200"]
    content_keys = list(resp["content"].keys())
    assert content_keys == ["text/event-stream"], (
        f"Expected text/event-stream, got {content_keys}")
    # The schema for SSE is type:string (the raw event data).
    assert resp["content"]["text/event-stream"]["schema"] == {"type": "string"}


def test_non_stream_route_openapi_media_type():
    """Non-stream routes must still advertise application/json."""
    from lancelot.app import App

    app = App()

    @app.get("/hello")
    async def hello():
        return {"msg": "hi"}

    spec = app.openapi()
    resp = spec["paths"]["/hello"]["get"]["responses"]["200"]
    assert "application/json" in resp["content"]
    assert "text/event-stream" not in resp["content"]


def test_stream_route_method_is_get():
    """Sanity: @app.stream registers a GET route."""
    from lancelot.app import App
    from lancelot.stream import Event

    app = App()

    @app.stream("/events")
    async def events():
        yield Event(data="x")

    spec = app.openapi()
    assert "get" in spec["paths"]["/events"]


# ---------------------------------------------------------------------------
# A-T1.2: servers / description / openapi_tags forwarding
# ---------------------------------------------------------------------------

def test_build_openapi_servers():
    """servers list is forwarded into the spec."""
    from lancelot.app import App

    app = App(servers=[{"url": "https://api.example.com", "description": "Prod"}])
    spec = app.openapi()
    assert "servers" in spec
    assert spec["servers"] == [{"url": "https://api.example.com",
                                  "description": "Prod"}]


def test_build_openapi_description():
    """description is forwarded into info."""
    from lancelot.app import App

    app = App(description="My AI service")
    spec = app.openapi()
    assert spec["info"]["description"] == "My AI service"


def test_build_openapi_openapi_tags():
    """openapi_tags are forwarded as a top-level tags list."""
    from lancelot.app import App

    tags = [{"name": "users", "description": "User operations"},
            {"name": "items"}]
    app = App(openapi_tags=tags)
    spec = app.openapi()
    assert spec["tags"] == tags


def test_build_openapi_no_extra_keys_when_unset():
    """When servers/description/openapi_tags are unset the keys are absent."""
    from lancelot.app import App

    app = App()  # no extra kwargs
    spec = app.openapi()
    assert "servers" not in spec
    assert "tags" not in spec
    # description key must not appear in info when unset.
    assert "description" not in spec.get("info", {})


def test_app_stores_openapi_meta():
    """App attributes are accessible directly."""
    from lancelot.app import App

    app = App(
        description="desc",
        servers=[{"url": "http://localhost"}],
        openapi_tags=[{"name": "ops"}],
    )
    assert app.description == "desc"
    assert app.servers == [{"url": "http://localhost"}]
    assert app.openapi_tags == [{"name": "ops"}]


# ---------------------------------------------------------------------------
# A-T1.3: OAuth2AuthorizationCodeBearer import + scheme
# ---------------------------------------------------------------------------

def test_oauth2_authorization_code_bearer_importable():
    """OAuth2AuthorizationCodeBearer is importable from lancelot.security."""
    from lancelot.security import OAuth2AuthorizationCodeBearer  # noqa: F401


def test_oauth2_authorization_code_bearer_scheme():
    """OAuth2AuthorizationCodeBearer produces the correct OpenAPI model."""
    from lancelot.security import OAuth2AuthorizationCodeBearer

    dep = OAuth2AuthorizationCodeBearer(
        authorizationUrl="https://auth.example.com/authorize",
        tokenUrl="https://auth.example.com/token",
        scopes={"read:users": "Read users"},
    )
    assert dep.model["type"] == "oauth2"
    assert "authorizationCode" in dep.model["flows"]
    flow = dep.model["flows"]["authorizationCode"]
    assert flow["authorizationUrl"] == "https://auth.example.com/authorize"
    assert flow["tokenUrl"] == "https://auth.example.com/token"
    assert flow["scopes"] == {"read:users": "Read users"}


def test_oauth2_authorization_code_bearer_refresh_url():
    """refreshUrl is included in the flow when provided."""
    from lancelot.security import OAuth2AuthorizationCodeBearer

    dep = OAuth2AuthorizationCodeBearer(
        authorizationUrl="https://auth.example.com/authorize",
        tokenUrl="https://auth.example.com/token",
        refreshUrl="https://auth.example.com/refresh",
    )
    flow = dep.model["flows"]["authorizationCode"]
    assert flow["refreshUrl"] == "https://auth.example.com/refresh"


def test_oauth2_authorization_code_bearer_extracts_token():
    """Bearer token is extracted from the Authorization header."""
    from lancelot.security import OAuth2AuthorizationCodeBearer

    dep = OAuth2AuthorizationCodeBearer(
        authorizationUrl="https://auth.example.com/authorize",
        tokenUrl="https://auth.example.com/token",
    )

    class FakeRequest:
        headers = {"Authorization": "Bearer my-token-abc"}

    result = asyncio.run(dep(FakeRequest()))
    assert result == "my-token-abc"


def test_oauth2_authorization_code_bearer_401_on_missing():
    """Missing token → 401 when auto_error=True."""
    from lancelot.exceptions import HTTPException
    from lancelot.security import OAuth2AuthorizationCodeBearer

    dep = OAuth2AuthorizationCodeBearer(
        authorizationUrl="https://auth.example.com/authorize",
        tokenUrl="https://auth.example.com/token",
    )

    class FakeRequest:
        headers = {}

    async def run():
        return await dep(FakeRequest())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 401


def test_oauth2_authorization_code_bearer_auto_error_false():
    """auto_error=False returns None instead of raising."""
    from lancelot.security import OAuth2AuthorizationCodeBearer

    dep = OAuth2AuthorizationCodeBearer(
        authorizationUrl="https://auth.example.com/authorize",
        tokenUrl="https://auth.example.com/token",
        auto_error=False,
    )

    class FakeRequest:
        headers = {}

    result = asyncio.run(dep(FakeRequest()))
    assert result is None


# ---------------------------------------------------------------------------
# A-T1.3: OpenIdConnect import + scheme
# ---------------------------------------------------------------------------

def test_openid_connect_importable():
    """OpenIdConnect is importable from lancelot.security."""
    from lancelot.security import OpenIdConnect  # noqa: F401


def test_openid_connect_scheme():
    """OpenIdConnect produces the correct OpenAPI model."""
    from lancelot.security import OpenIdConnect

    dep = OpenIdConnect(
        openIdConnectUrl="https://auth.example.com/.well-known/openid-configuration"
    )
    assert dep.model["type"] == "openIdConnect"
    assert dep.model["openIdConnectUrl"] == (
        "https://auth.example.com/.well-known/openid-configuration"
    )


def test_openid_connect_returns_auth_header():
    """OpenIdConnect returns the raw Authorization header."""
    from lancelot.security import OpenIdConnect

    dep = OpenIdConnect(
        openIdConnectUrl="https://auth.example.com/.well-known/openid-configuration"
    )

    class FakeRequest:
        headers = {"Authorization": "Bearer oidc-token"}

    result = asyncio.run(dep(FakeRequest()))
    assert result == "Bearer oidc-token"


def test_openid_connect_401_on_missing():
    """Missing auth → 401 when auto_error=True."""
    from lancelot.exceptions import HTTPException
    from lancelot.security import OpenIdConnect

    dep = OpenIdConnect(
        openIdConnectUrl="https://auth.example.com/.well-known/openid-configuration"
    )

    class FakeRequest:
        headers = {}

    async def run():
        return await dep(FakeRequest())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# A-T1.3: OAuth2PasswordRequestFormStrict
# ---------------------------------------------------------------------------

def test_oauth2_password_request_form_strict_importable():
    """OAuth2PasswordRequestFormStrict is importable from lancelot.security."""
    from lancelot.security import OAuth2PasswordRequestFormStrict  # noqa: F401


def test_oauth2_password_request_form_strict_accepts_password():
    """Strict form accepts grant_type='password'."""
    from lancelot.security import OAuth2PasswordRequestFormStrict

    form = OAuth2PasswordRequestFormStrict(
        grant_type="password", username="alice", password="s3cr3t", scope="read write"
    )
    assert form.grant_type == "password"
    assert form.username == "alice"
    assert form.scopes == ["read", "write"]


def test_oauth2_password_request_form_strict_rejects_other_grant():
    """Strict form raises 422 for non-password grant_type."""
    from lancelot.exceptions import HTTPException
    from lancelot.security import OAuth2PasswordRequestFormStrict

    with pytest.raises(HTTPException) as exc:
        OAuth2PasswordRequestFormStrict(
            grant_type="client_credentials", username="", password=""
        )
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# A-T1.3: Security schemes in generated OpenAPI spec
# ---------------------------------------------------------------------------

def test_oauth2_authorization_code_bearer_in_openapi_spec():
    """OAuth2AuthorizationCodeBearer scheme appears in the generated spec."""
    from lancelot.app import App
    from lancelot.params import Security
    from lancelot.security import OAuth2AuthorizationCodeBearer

    app = App()
    scheme = OAuth2AuthorizationCodeBearer(
        authorizationUrl="https://auth.example.com/authorize",
        tokenUrl="https://auth.example.com/token",
    )

    @app.get("/protected")
    async def protected(token=Security(scheme)):
        return {"token": token}

    spec = app.openapi()
    assert "securitySchemes" in spec.get("components", {})
    schemes = spec["components"]["securitySchemes"]
    assert "OAuth2AuthorizationCodeBearer" in schemes
    m = schemes["OAuth2AuthorizationCodeBearer"]
    assert m["type"] == "oauth2"
    assert "authorizationCode" in m["flows"]


def test_openid_connect_in_openapi_spec():
    """OpenIdConnect scheme appears in the generated spec."""
    from lancelot.app import App
    from lancelot.params import Security
    from lancelot.security import OpenIdConnect

    app = App()
    scheme = OpenIdConnect(
        openIdConnectUrl="https://auth.example.com/.well-known/openid-configuration"
    )

    @app.get("/oidc-protected")
    async def oidc_protected(cred=Security(scheme)):
        return {"cred": cred}

    spec = app.openapi()
    assert "securitySchemes" in spec.get("components", {})
    schemes = spec["components"]["securitySchemes"]
    assert "OpenIdConnect" in schemes
    m = schemes["OpenIdConnect"]
    assert m["type"] == "openIdConnect"


# ---------------------------------------------------------------------------
# A-T2.1: sync yield-dep __exit__ runs off the event loop
# ---------------------------------------------------------------------------

def test_sync_yield_dep_exit_runs_in_threadpool():
    """_sync_cm_in_threadpool wraps a sync CM so __exit__ runs off the loop.
    The cleanup thread-id must differ from the event-loop thread-id."""
    from lancelot.di import _sync_cm_in_threadpool

    loop_thread_id_holder = []
    exit_thread_id_holder = []

    class TrackingCM:
        def __enter__(self):
            return "value"

        def __exit__(self, *exc_info):
            exit_thread_id_holder.append(threading.current_thread().ident)
            return False

    async def run():
        loop_thread_id_holder.append(threading.current_thread().ident)
        async with _sync_cm_in_threadpool(TrackingCM()) as val:
            assert val == "value"

    asyncio.run(run())

    assert exit_thread_id_holder, "__exit__ was never called"
    assert loop_thread_id_holder, "loop thread id not captured"
    assert exit_thread_id_holder[0] != loop_thread_id_holder[0], (
        "__exit__ ran ON the event loop — should run off-loop")


def test_sync_yield_dep_exit_exception_path():
    """_sync_cm_in_threadpool propagates exceptions correctly."""
    from lancelot.di import _sync_cm_in_threadpool

    entered = []
    exited_with_exc = []

    class TrackingCM:
        def __enter__(self):
            entered.append(True)
            return "val"

        def __exit__(self, tp, exc, tb):
            exited_with_exc.append(tp)
            return False  # do not suppress

    async def run():
        async with _sync_cm_in_threadpool(TrackingCM()):
            raise ValueError("test error")

    with pytest.raises(ValueError, match="test error"):
        asyncio.run(run())

    assert entered
    assert exited_with_exc == [ValueError]


def test_sync_yield_dep_via_di_solve_async():
    """Verify sync yield-dep teardown works end-to-end via solve_async."""
    from lancelot.di import compile_dependant, solve_async
    from lancelot.params import Depends

    cleanup_ran = []

    def get_db():
        yield "db-connection"
        cleanup_ran.append("cleaned")

    async def handler_with_dep(db=Depends(get_db)):
        return db

    plan = compile_dependant(handler_with_dep)

    class FakeRequest:
        path_params = {}
        query_params = {}
        headers = {}
        cookies = {}
        body = b""
        query_params_multi = {}
        _background = None
        _ws = None
        headers_multi = {}
        app = None

    async def run():
        async with solve_async(plan, FakeRequest()) as sr:
            assert not sr.errors
            assert sr.kwargs.get("db") == "db-connection"

    asyncio.run(run())

    assert cleanup_ran == ["cleaned"], (
        "sync yield-dep __exit__ (cleanup) did not run")


def test_sync_yield_dep_suppress_suppresses():
    """If the sync CM's __exit__ returns True, the exception is suppressed."""
    from lancelot.di import _sync_cm_in_threadpool

    class SuppressingCM:
        def __enter__(self):
            return "x"

        def __exit__(self, *args):
            return True  # suppress

    async def run():
        # Should NOT raise even though we raise inside the block
        async with _sync_cm_in_threadpool(SuppressingCM()):
            raise RuntimeError("suppressed!")

    asyncio.run(run())  # must not raise
