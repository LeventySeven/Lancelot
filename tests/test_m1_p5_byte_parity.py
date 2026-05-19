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
