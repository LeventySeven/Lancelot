import asyncio

import lancelot._native as n


def test_dispatch_releases_gil_across_await():
    async def slow(req):
        await asyncio.sleep(0.1)
        return {"echo": req.json()}

    fi = n.FnInfo(slow, True, 1)
    # _drive runs N dispatches concurrently on Lancelot's runtime and returns
    # (results, wall_seconds). If the GIL were held across the await, 50*0.1s
    # would serialize to ~5s; released, it stays ~0.1-0.3s.
    results, wall = n._drive(fi, b'{"a":1}', 50)
    assert results[0] == b'{"echo":{"a":1}}'
    assert wall < 1.0, f"GIL not released across await (wall={wall}s)"
