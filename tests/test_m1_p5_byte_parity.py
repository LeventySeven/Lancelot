"""M1-P5 oracle: bounded blocking pool (U32 — fixes the FastAPI sync
cliff) + BackgroundTasks/background.spawn (U33). Sync handlers/deps run
OFF the event loop on a bounded stdlib pool; overload → byte 503
Retry-After (no hang); BackgroundTasks byte/timing-identical to real
fastapi 0.136.1. Stdlib-only (runtime dep = pydantic)."""
import asyncio
import threading
import time

import pytest


def test_pool_runs_off_the_event_loop_and_bounds_inflight():
    from lancelot.pool import BlockingPool, PoolFull

    pool = BlockingPool(max_workers=2, max_inflight=2)
    try:
        loop_thread = threading.get_ident()

        async def main():
            tid = await pool.offload(threading.get_ident)
            assert tid != loop_thread

            started = threading.Event()
            release = threading.Event()

            def block():
                started.set()
                release.wait(2)
                return "done"

            a = asyncio.create_task(pool.offload(block))
            b = asyncio.create_task(pool.offload(block))
            await asyncio.sleep(0.05)
            with pytest.raises(PoolFull):
                await pool.offload(lambda: 1)
            release.set()
            assert await a == "done" and await b == "done"

        asyncio.run(main())
    finally:
        pool.shutdown()


def test_sync_handlers_run_concurrently_and_bytes_match_fastapi():
    import fastapi
    from fastapi.testclient import TestClient as FA
    import httpx
    import lancelot
    from lancelot.testclient import TestClient as LC

    def build(F):
        app = F.FastAPI() if hasattr(F, "FastAPI") else F.App()

        @app.get("/slow")
        def slow():                       # SYNC handler
            time.sleep(0.25)
            return {"ok": True}

        return app

    with FA(build(fastapi)) as fa, LC(build(lancelot)) as lc:
        f, l = fa.get("/slow"), lc.get("/slow")
        assert l.status_code == f.status_code == 200
        assert l.content == f.content

    lapp = lancelot.App()

    @lapp.get("/slow")
    def slow2():
        time.sleep(0.25)
        return {"ok": True}

    async def hammer(app_callable):
        transport = httpx.ASGITransport(app=app_callable)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            t0 = time.perf_counter()
            await asyncio.gather(*[c.get("/slow") for _ in range(8)])
            return time.perf_counter() - t0

    elapsed = asyncio.run(hammer(lapp.asgi()))
    assert elapsed < 1.5, f"sync handlers serialized: {elapsed:.2f}s"


def test_sync_dependency_offloaded_and_bytes_match_fastapi():
    import fastapi
    from fastapi.testclient import TestClient as FA
    import httpx
    import lancelot
    from lancelot.testclient import TestClient as LC
    from lancelot.params import Depends as LDepends

    def build(F):
        is_fa = hasattr(F, "FastAPI")
        app = F.FastAPI() if is_fa else F.App()
        Dep = F.Depends if is_fa else LDepends

        def slow_dep():               # SYNC dependency
            time.sleep(0.25)
            return {"d": 1}

        @app.get("/d")
        def d(v=Dep(slow_dep)):
            return v

        return app

    with FA(build(fastapi)) as fa, LC(build(lancelot)) as lc:
        f, l = fa.get("/d"), lc.get("/d")
        assert l.status_code == f.status_code == 200
        assert l.content == f.content

    lapp = lancelot.App()

    def slow_dep2():
        time.sleep(0.25)
        return {"d": 1}

    @lapp.get("/d")
    def d2(v=LDepends(slow_dep2)):
        return v

    async def hammer():
        tr = httpx.ASGITransport(app=lapp.asgi())
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
            t0 = time.perf_counter()
            await asyncio.gather(*[c.get("/d") for _ in range(8)])
            return time.perf_counter() - t0

    assert asyncio.run(hammer()) < 1.5, "sync deps serialized"


def test_pool_full_returns_503_retry_after_not_a_hang():
    import httpx
    import lancelot
    from lancelot.pool import configure
    from lancelot.testclient import TestClient as LC  # noqa: F401

    configure(max_workers=1, max_inflight=1)  # tiny pool → easy to exhaust
    try:
        app = lancelot.App()

        @app.get("/blk")
        def blk():
            time.sleep(0.4)
            return {"ok": True}

        async def race():
            tr = httpx.ASGITransport(app=app.asgi())
            async with httpx.AsyncClient(transport=tr,
                                         base_url="http://t") as c:
                return await asyncio.gather(c.get("/blk"), c.get("/blk"),
                                            c.get("/blk"))

        rs = asyncio.run(race())
        codes = sorted(r.status_code for r in rs)
        assert 503 in codes, codes                       # not a hang
        over = [r for r in rs if r.status_code == 503][0]
        assert over.headers.get("retry-after") == "1"
        assert over.json() == {"detail": "Service Unavailable"}
    finally:
        configure()  # restore defaults for the rest of the suite


def test_background_tasks_run_after_response_like_fastapi():
    import fastapi
    from fastapi.testclient import TestClient as FA
    import lancelot
    from lancelot.testclient import TestClient as LC

    def build(F, sink):
        is_fa = hasattr(F, "FastAPI")
        app = F.FastAPI() if is_fa else F.App()
        BT = (fastapi.BackgroundTasks if is_fa
              else __import__("lancelot", fromlist=["BackgroundTasks"]
                              ).BackgroundTasks)

        def write(tag):
            sink.append(tag)

        @app.get("/bg")
        def bg(bt: BT):
            bt.add_task(write, "first")
            bt.add_task(write, "second")
            return {"queued": 2}

        return app

    fsink, lsink = [], []
    with FA(build(fastapi, fsink)) as fa, LC(build(lancelot, lsink)) as lc:
        f, l = fa.get("/bg"), lc.get("/bg")
        assert l.status_code == f.status_code == 200
        assert l.content == f.content                  # byte parity
        assert lsink == fsink == ["first", "second"]   # order + ran-after
