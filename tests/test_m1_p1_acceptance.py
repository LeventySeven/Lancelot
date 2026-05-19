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
    """M1-P7 contract: the conformance instrument runs over the REAL
    pinned FastAPI corpus (no longer the M1-P1 11-file seed), and the
    measured Class-A pct is >= parity_floor.txt (the ratchet's only-
    rises contract). M1-P1's `passed == total` was a seed-era
    placeholder; P7's honest contract is: the floor holds."""
    import pathlib

    from conformance.runner import run

    passed, total = run()
    pct = 100.0 * passed / total
    floor = float((pathlib.Path("conformance") / "parity_floor.txt"
                   ).read_text().strip())
    assert total >= 100 and pct + 1e-9 >= floor, (passed, total, pct, floor)
