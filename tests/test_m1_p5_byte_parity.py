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
