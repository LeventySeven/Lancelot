def test_fastapi_compat_surface():
    import lancelot
    from lancelot import App, TestClient
    from lancelot.compat import APIRouter, FastAPI

    assert FastAPI is App
    app = FastAPI()
    r = APIRouter(prefix="/x")

    @r.get("/y")
    def y():
        return {"ok": 1}

    app.include_router(r)
    with TestClient(app) as c:
        assert c.get("/x/y").json() == {"ok": 1}
    assert isinstance(lancelot.__version__, str)
