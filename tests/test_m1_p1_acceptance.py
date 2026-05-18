def test_p1_dod_surface():
    import lancelot
    from lancelot import App, TestClient
    from lancelot.compat import FastAPI

    assert FastAPI is App
    app = App()

    @app.get("/h")
    def h():
        return {"p1": True}

    with TestClient(app) as c:
        assert c.get("/h").json() == {"p1": True}
        assert c.get("/nope").status_code == 404


def test_p1_conformance_instrument_runs():
    from conformance.runner import run

    passed, total = run()
    assert total >= 4 and passed == total  # seed = spine-only, all green
