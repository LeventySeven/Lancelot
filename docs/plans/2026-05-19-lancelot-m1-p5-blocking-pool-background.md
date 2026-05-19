# Lancelot M1-P5 — Blocking Pool + Background Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline-serial, the M1 chosen mode). Steps use checkbox (`- [ ]`) syntax. TDD is mandatory: RED → verify-fail → GREEN → verify-pass → exit-gated commit.

**Goal:** Sync handlers and sync dependencies no longer block the event loop (they run on a bounded, GIL-friendly thread pool); on pool exhaustion the request gets a byte-exact `503 Retry-After` instead of a silent hang; `BackgroundTasks` and a detached `background.spawn` pool let work outlive the response without delaying it — fixing the two FastAPI cliffs (WBS U32/U33) while staying byte-identical to FastAPI on the response.

**Architecture:** The conformance/byte-oracle path is the pure-Python ASGI seam (`lancelot/asgi.py` + `lancelot/di.py`); that is where the observable behavior lives and is verified. Offload uses the **standard library only** (`asyncio` loop default executor is replaced by a bounded `concurrent.futures.ThreadPoolExecutor` plus an inflight gate) — the runtime dependency set stays `pydantic` only (anyio/starlette are TEST-only). The Rust `src/blocking/runner.rs` / `src/background/pool.rs` relocation belongs to the spec §3-69 Rust-pipeline unification and is ledgered RED, not built against a harness that cannot reach it (honest scoping, same discipline as M1-P3/P4).

**Tech Stack:** Python 3.13, stdlib `asyncio` + `concurrent.futures.ThreadPoolExecutor` + `threading`, pydantic-core wire seam (unchanged), real `fastapi==0.136.1` / `starlette==1.0.0` as the byte-differential oracle (TEST-only).

---

## File Structure

- Create: `lancelot/pool.py` — the bounded blocking pool: a singleton `BlockingPool` (bounded `ThreadPoolExecutor`, bounded inflight counter), `offload(fn, /, *a, **kw)` coroutine, `PoolFull` exception, `configure()` for size/queue, `shutdown()`. One responsibility: run sync callables off the event loop with bounded backpressure.
- Create: `lancelot/background.py` — `BackgroundTasks` (FastAPI-compatible: `.add_task`, runs after response), and the detached `background` pool (`background.spawn`, separate executor, bounded queue, `await_in_request` opt-in). One responsibility: post-response / detached work.
- Modify: `lancelot/asgi.py` — offload sync handlers; map `PoolFull` → byte `503 Retry-After`; run `BackgroundTasks` after the response body is sent.
- Modify: `lancelot/di.py` — offload sync plain dependencies in `solve_async`; recognize the `BackgroundTasks` parameter at compile time and bind it.
- Modify: `lancelot/__init__.py` — lazily export `BackgroundTasks`, `background` (PEP 562, dependency-light).
- Test: `tests/test_m1_p5_byte_parity.py` — real-FastAPI byte/behavior differential.
- Test/seed: `conformance/vendor/quarantine/test_p5_longtail.py` — honest RED ledger for the genuinely-deferred tail.

---

### Task 1: Bounded blocking pool (`lancelot/pool.py`)

**Files:**
- Create: `lancelot/pool.py`
- Test: `tests/test_m1_p5_byte_parity.py` (shared file; this task adds `test_pool_*`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_m1_p5_byte_parity.py
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
            # the sync fn must NOT run on the loop thread
            tid = await pool.offload(threading.get_ident)
            assert tid != loop_thread

            # max_inflight=2 → a 3rd concurrent submit raises PoolFull
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py::test_pool_runs_off_the_event_loop_and_bounds_inflight -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'lancelot.pool'`.

- [ ] **Step 3: Write minimal implementation**

```python
# lancelot/pool.py
"""Bounded, GIL-friendly blocking pool (WBS U32 — "fixes the FastAPI
cliff"). Stdlib only: the runtime dep set is `pydantic` (anyio is a
TEST-only dep). Sync handlers/deps run HERE off the event loop; when the
bounded inflight budget is exhausted the caller gets PoolFull, which the
ASGI seam maps to a byte-exact 503 Retry-After (no silent hang — unlike
FastAPI/anyio which backpressure at 40 and can stall the reactor).

The Rust 3-variant Empty/Mono/Pool runner (src/blocking/runner.rs) and
crossbeam BOUNDED(10_000) live with the spec §3-69 Rust-pipeline
unification; this is the Python-ASGI-seam realization that the
conformance/byte oracle actually exercises (ledger: conformance/vendor/
quarantine/test_p5_longtail.py)."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor


class PoolFull(Exception):
    """Raised when the bounded inflight budget is exhausted. The ASGI seam
    maps this to 503 + Retry-After (WBS U32 done-when)."""


class BlockingPool:
    def __init__(self, max_workers: int = 40, max_inflight: int = 10_000):
        # FastAPI/anyio default thread budget is 40 — match it so default
        # latency is on par; max_inflight is the queue ceiling (WBS:
        # crossbeam BOUNDED(10_000)) that turns an overload into a 503
        # instead of an unbounded backlog.
        self._ex = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="lancelot-blocking")
        self._max_inflight = max_inflight
        self._inflight = 0
        self._lock = threading.Lock()

    async def offload(self, fn, /, *args, **kwargs):
        with self._lock:
            if self._inflight >= self._max_inflight:
                raise PoolFull()
            self._inflight += 1
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                self._ex, lambda: fn(*args, **kwargs))
        finally:
            with self._lock:
                self._inflight -= 1

    def shutdown(self):
        self._ex.shutdown(wait=False, cancel_futures=True)


_DEFAULT: BlockingPool | None = None
_DEFAULT_LOCK = threading.Lock()


def get_pool() -> BlockingPool:
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                _DEFAULT = BlockingPool()
    return _DEFAULT


def configure(*, max_workers: int = 40, max_inflight: int = 10_000) -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is not None:
            _DEFAULT.shutdown()
        _DEFAULT = BlockingPool(max_workers=max_workers,
                                max_inflight=max_inflight)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py::test_pool_runs_off_the_event_loop_and_bounds_inflight -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
LC=/Users/seventyleven/Desktop/Lancelot
git -C "$LC" add lancelot/pool.py tests/test_m1_p5_byte_parity.py
.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py -q >/dev/null 2>&1
rc=$?; test $rc -eq 0 && git -C "$LC" commit -q -m "feat(m1-p5): bounded stdlib blocking pool (U32) + PoolFull"
```

---

### Task 2: Offload sync handlers (`lancelot/asgi.py`)

**Files:**
- Modify: `lancelot/asgi.py:145-147` (the handler-call seam)
- Test: `tests/test_m1_p5_byte_parity.py` (adds `test_sync_handler_*`)

- [ ] **Step 1: Write the failing test**

```python
def test_sync_handlers_run_concurrently_and_bytes_match_fastapi():
    import fastapi
    from fastapi.testclient import TestClient as FA
    import lancelot
    from lancelot.testclient import TestClient as LC

    def build(F):
        app = F.FastAPI() if hasattr(F, "FastAPI") else F.App()

        @app.get("/slow")
        def slow():                       # SYNC handler
            time.sleep(0.25)
            return {"ok": True}

        return app

    # byte parity: a sync handler's response is identical to FastAPI
    with FA(build(fastapi)) as fa, LC(build(lancelot)) as lc:
        f, l = fa.get("/slow"), lc.get("/slow")
        assert l.status_code == f.status_code == 200
        assert l.content == f.content

    # concurrency: 8 concurrent sync requests must NOT serialize
    # (8 * 0.25s = 2.0s if serialized; well under if offloaded)
    import httpx
    from starlette.testclient import TestClient as STC

    async def hammer(app_callable):
        transport = httpx.ASGITransport(app=app_callable)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            t0 = time.perf_counter()
            await asyncio.gather(*[c.get("/slow") for _ in range(8)])
            return time.perf_counter() - t0

    lapp = lancelot.App()

    @lapp.get("/slow")
    def slow2():
        time.sleep(0.25)
        return {"ok": True}

    elapsed = asyncio.run(hammer(lapp.asgi()))
    assert elapsed < 1.5, f"sync handlers serialized: {elapsed:.2f}s"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py::test_sync_handlers_run_concurrently_and_bytes_match_fastapi -q`
Expected: FAIL — `elapsed` ≈ 2.0s (asgi.py:145 calls `route.handler(**kwargs)` inline on the loop, serializing every sync request).

- [ ] **Step 3: Write minimal implementation**

In `lancelot/asgi.py`, replace the handler-call seam:

```python
            result = route.handler(**kwargs)
            if inspect.isawaitable(result):
                result = await result
```

with:

```python
            # Sync handlers run on the bounded blocking pool so they never
            # pin the reactor (WBS U32 — the FastAPI cliff). Async handlers
            # are awaited directly. iscoroutinefunction is decided here, not
            # by calling-then-checking, so a sync fn never runs on the loop.
            from lancelot.pool import PoolFull, get_pool

            if route.plan.is_async:
                result = await route.handler(**kwargs)
            else:
                result = await get_pool().offload(route.handler, **kwargs)
```

(`route.plan.is_async` is already computed at compile time — `Plan.is_async = inspect.iscoroutinefunction(fn)` in `di.py:85-86`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py::test_sync_handlers_run_concurrently_and_bytes_match_fastapi -q`
Expected: PASS (elapsed < 1.5s; bytes equal FastAPI).

- [ ] **Step 5: Commit**

```bash
LC=/Users/seventyleven/Desktop/Lancelot
git -C "$LC" add lancelot/asgi.py tests/test_m1_p5_byte_parity.py
.venv/bin/python -m pytest tests/ -q >/dev/null 2>&1
rc=$?; test $rc -eq 0 && git -C "$LC" commit -q -m "feat(m1-p5): offload sync handlers to the blocking pool (U32)"
```

---

### Task 3: Offload sync dependencies (`lancelot/di.py`)

**Files:**
- Modify: `lancelot/di.py:153` (sync path in `solve`) is the SYNC driver — leave it; the async path `solve_async:219` (`val = target(**sr.kwargs)`) is what the ASGI seam uses.
- Test: `tests/test_m1_p5_byte_parity.py` (adds `test_sync_dep_*`)

- [ ] **Step 1: Write the failing test**

```python
def test_sync_dependency_offloaded_and_bytes_match_fastapi():
    import fastapi
    from fastapi.testclient import TestClient as FA
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

    import httpx
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py::test_sync_dependency_offloaded_and_bytes_match_fastapi -q`
Expected: FAIL — serialized (~2.0s); `di.py` `solve_async` runs the plain sync dep inline (`val = target(**sr.kwargs)`).

- [ ] **Step 3: Write minimal implementation**

In `lancelot/di.py` `solve_async`, the plain-dep `else` branch currently:

```python
                else:
                    val = target(**sr.kwargs)
```

becomes (offload only genuinely-sync, non-generator deps; async/gen/asyncgen branches above are unchanged — generator-context deps must stay on the loop for the AsyncExitStack/`gen.throw` teardown semantics, exactly as FastAPI keeps them):

```python
                else:
                    if inspect.iscoroutinefunction(target):
                        val = target(**sr.kwargs)
                        val = await val
                    else:
                        from lancelot.pool import get_pool

                        val = await get_pool().offload(target, **sr.kwargs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py::test_sync_dependency_offloaded_and_bytes_match_fastapi -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite (no regression — yield-dep teardown B4 must still hold)**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass (the M1-P2 yield-dep/`use_cache`/override tests are unaffected — only the plain-sync branch changed).

- [ ] **Step 6: Commit**

```bash
LC=/Users/seventyleven/Desktop/Lancelot
git -C "$LC" add lancelot/di.py tests/test_m1_p5_byte_parity.py
.venv/bin/python -m pytest tests/ -q >/dev/null 2>&1
rc=$?; test $rc -eq 0 && git -C "$LC" commit -q -m "feat(m1-p5): offload sync plain deps (FastAPI run_in_threadpool parity)"
```

---

### Task 4: Pool-full → byte-exact `503 Retry-After` (`lancelot/asgi.py`)

**Files:**
- Modify: `lancelot/asgi.py` (the `try/except` around `solve_async`/handler)
- Test: `tests/test_m1_p5_byte_parity.py` (adds `test_pool_full_503`)

- [ ] **Step 1: Write the failing test**

```python
def test_pool_full_returns_503_retry_after_not_a_hang():
    import lancelot
    from lancelot.pool import configure
    from lancelot.testclient import TestClient as LC

    configure(max_workers=1, max_inflight=1)  # tiny pool → easy to exhaust
    try:
        app = lancelot.App()

        @app.get("/blk")
        def blk():
            time.sleep(0.4)
            return {"ok": True}

        import httpx

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py::test_pool_full_returns_503_retry_after_not_a_hang -q`
Expected: FAIL — `PoolFull` is currently caught by the generic `except Exception` → 500, not a 503 with `Retry-After`.

- [ ] **Step 3: Write minimal implementation**

In `lancelot/asgi.py`, add a `PoolFull` handler BEFORE the generic `except Exception` (mirroring the existing `except HTTPException` placement). Add the import next to the existing `from lancelot.exceptions import HTTPException`:

```python
    from lancelot.pool import PoolFull
```

and the handler (immediately before `except Exception:`):

```python
    except PoolFull:  # WBS U32: overload → byte 503 + Retry-After, no hang
        if not sent:
            pbody = b'{"detail":"Service Unavailable"}'
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"retry-after", b"1"),
                                    (b"content-length",
                                     str(len(pbody)).encode()),
                                    (b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": pbody})
```

(Header order follows the M1-P4 Starlette `init_headers` invariant: explicit header first, then content-length, then content-type.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py::test_pool_full_returns_503_retry_after_not_a_hang -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
LC=/Users/seventyleven/Desktop/Lancelot
git -C "$LC" add lancelot/asgi.py tests/test_m1_p5_byte_parity.py
.venv/bin/python -m pytest tests/ -q >/dev/null 2>&1
rc=$?; test $rc -eq 0 && git -C "$LC" commit -q -m "feat(m1-p5): pool-full -> byte 503 Retry-After (no silent hang)"
```

---

### Task 5: `BackgroundTasks` parameter — FastAPI byte/timing parity

**Files:**
- Create: `lancelot/background.py` (the `BackgroundTasks` class half)
- Modify: `lancelot/di.py` (`compile_dependant`: recognize the `BackgroundTasks` annotation), `lancelot/asgi.py` (run them after the response body), `lancelot/__init__.py` (lazy export)
- Test: `tests/test_m1_p5_byte_parity.py` (adds `test_background_tasks_*`)

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py::test_background_tasks_run_after_response_like_fastapi -q`
Expected: FAIL — `cannot import name 'BackgroundTasks' from 'lancelot'`.

- [ ] **Step 3: Write minimal implementation**

`lancelot/background.py` (class half — the detached pool is Task 6):

```python
"""BackgroundTasks (FastAPI byte/timing parity — tasks run AFTER the
response is sent) + the detached background pool (WBS U33, Task 6).
Sync tasks run on the M1-P5 blocking pool so they never pin the reactor;
FastAPI runs them via run_in_threadpool too."""
import inspect


class BackgroundTasks:
    def __init__(self):
        self._tasks = []

    def add_task(self, func, /, *args, **kwargs):
        self._tasks.append((func, args, kwargs))

    async def __call__(self):
        from lancelot.pool import get_pool

        for func, args, kwargs in self._tasks:
            if inspect.iscoroutinefunction(func):
                await func(*args, **kwargs)
            else:
                await get_pool().offload(func, *args, **kwargs)
```

In `lancelot/di.py` `compile_dependant`, recognize the parameter by annotation (same shape as the existing `if ann is _Req:` Request branch, placed right after it):

```python
        from lancelot.background import BackgroundTasks as _BT

        if ann is _BT:  # FastAPI: a fresh BackgroundTasks per request
            req_params.append(("__bg__", name))
            continue
```

Change `req_params` handling so a plain `Request` stays a bare name while a
background slot is a tagged tuple. In `compile_dependant` keep
`req_params.append(name)` for the Request branch; for binding, in BOTH
`solve` and `solve_async` replace the `for nm in plan.request_params:`
loop with:

```python
        for entry in plan.request_params:
            if isinstance(entry, tuple) and entry[0] == "__bg__":
                kwargs[entry[1]] = request._background  # set by the seam
            else:
                kwargs[entry] = request
```

In `lancelot/requests.py` add `self._background = None` in `Request.__init__`
(one line, next to `scope_state`).

In `lancelot/asgi.py`, before `async with solve_async(...)`, attach a fresh
container:

```python
    from lancelot.background import BackgroundTasks as _BT
    lreq._background = _BT()
```

and AFTER the success response is sent (both the `Response` passthrough
and the `_send_json` payload path — i.e. right after `sent = True` in the
success branch, still inside the `async with`), run them:

```python
            if lreq._background._tasks:
                await lreq._background()
```

In `lancelot/__init__.py` extend the lazy `__getattr__`:

```python
    if name == "BackgroundTasks":
        from lancelot.background import BackgroundTasks

        return BackgroundTasks
```

and add `"BackgroundTasks"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py::test_background_tasks_run_after_response_like_fastapi -q`
Expected: PASS.

- [ ] **Step 5: Run full suite (no regression — Request DI binding changed)**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass (M1-P4 `Request`-injection tests still green — the bare-name path is preserved).

- [ ] **Step 6: Commit**

```bash
LC=/Users/seventyleven/Desktop/Lancelot
git -C "$LC" add lancelot/background.py lancelot/di.py lancelot/asgi.py \
  lancelot/requests.py lancelot/__init__.py tests/test_m1_p5_byte_parity.py
.venv/bin/python -m pytest tests/ -q >/dev/null 2>&1
rc=$?; test $rc -eq 0 && git -C "$LC" commit -q -m "feat(m1-p5): BackgroundTasks param (FastAPI byte/timing parity)"
```

---

### Task 6: Detached `background.spawn` pool (`lancelot/background.py`, WBS U33)

**Files:**
- Modify: `lancelot/background.py` (add the detached pool + `spawn`)
- Modify: `lancelot/__init__.py` (lazy export `background`)
- Test: `tests/test_m1_p5_byte_parity.py` (adds `test_background_spawn_*`)

- [ ] **Step 1: Write the failing test**

```python
def test_background_spawn_does_not_delay_the_response():
    import lancelot
    from lancelot.testclient import TestClient as LC

    done = threading.Event()

    app = lancelot.App()

    @app.get("/spawn")
    def spawn_route():
        from lancelot import background

        def long_job():
            time.sleep(0.6)
            done.set()

        background.spawn(long_job)            # detached, bounded
        return {"accepted": True}

    with LC(app) as lc:
        t0 = time.perf_counter()
        r = lc.get("/spawn")
        dt = time.perf_counter() - t0
        assert r.status_code == 200
        assert dt < 0.3, f"spawn delayed the response: {dt:.2f}s"  # U33
        assert not done.is_set()              # still running after response
        assert done.wait(2)                   # it does eventually complete


def test_background_spawn_bounded_queue_rejects_when_full():
    from lancelot.background import BackgroundPool, QueueFull

    bp = BackgroundPool(max_workers=1, max_queue=1)
    try:
        rel = threading.Event()
        bp.spawn(lambda: rel.wait(2))         # occupies the worker
        bp.spawn(lambda: None)                # fills the queue (depth 1)
        with pytest.raises(QueueFull):
            bp.spawn(lambda: None)            # over the bound → rejected
        rel.set()
    finally:
        bp.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py::test_background_spawn_does_not_delay_the_response tests/test_m1_p5_byte_parity.py::test_background_spawn_bounded_queue_rejects_when_full -q`
Expected: FAIL — no `spawn` on `background`, no `BackgroundPool`/`QueueFull`.

- [ ] **Step 3: Write minimal implementation**

Append to `lancelot/background.py`:

```python
import threading
from concurrent.futures import ThreadPoolExecutor


class QueueFull(Exception):
    """Detached background queue exhausted (WBS U33 bounded queue)."""


class BackgroundPool:
    """Detached, Rust-owned-in-spirit pool SEPARATE from request workers
    (WBS U33): a request returns immediately; the job outlives it. Bounded
    queue → QueueFull rather than unbounded growth. The Rust src/background/
    pool.rs realization is the spec §3-69 unification (ledgered RED)."""

    def __init__(self, max_workers: int = 8, max_queue: int = 10_000):
        self._ex = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="lancelot-bg")
        self._max_queue = max_queue
        self._depth = 0
        self._lock = threading.Lock()

    def spawn(self, fn, /, *args, **kwargs):
        with self._lock:
            if self._depth >= self._max_queue:
                raise QueueFull()
            self._depth += 1

        def _run():
            try:
                fn(*args, **kwargs)
            finally:
                with self._lock:
                    self._depth -= 1

        self._ex.submit(_run)

    def shutdown(self):
        self._ex.shutdown(wait=False, cancel_futures=True)


_POOL: BackgroundPool | None = None
_POOL_LOCK = threading.Lock()


def _pool() -> BackgroundPool:
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = BackgroundPool()
    return _POOL


def spawn(fn, /, *args, **kwargs):
    """Detached: returns immediately, the job outlives the response (U33).
    `await_in_request=True` opts back into the old run-before-return
    semantics (kept for migration parity)."""
    if kwargs.pop("await_in_request", False):
        fn(*args, **kwargs)
        return
    _pool().spawn(fn, *args, **kwargs)
```

Create a tiny module facade so `from lancelot import background` exposes
`spawn`. In `lancelot/__init__.py` `__getattr__`:

```python
    if name == "background":
        from lancelot import background as _bg

        return _bg
```

(`lancelot.background` is already an importable module; the lazy hook just
keeps `from lancelot import background` dependency-light and explicit.) Add
`"background"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py -q`
Expected: PASS (all P5 tests).

- [ ] **Step 5: Commit**

```bash
LC=/Users/seventyleven/Desktop/Lancelot
git -C "$LC" add lancelot/background.py lancelot/__init__.py \
  tests/test_m1_p5_byte_parity.py
.venv/bin/python -m pytest tests/ -q >/dev/null 2>&1
rc=$?; test $rc -eq 0 && git -C "$LC" commit -q -m "feat(m1-p5): detached background.spawn pool (U33) + QueueFull"
```

---

### Task 7: Real-FastAPI byte-differential consolidation + import-clean guard

**Files:**
- Modify: `tests/test_m1_p5_byte_parity.py` (add the consolidation + import-clean assertions)

- [ ] **Step 1: Write the failing test**

```python
def test_import_lancelot_stays_dependency_light_with_pool():
    # M0 DoD #1 regression guard: importing lancelot (and touching the
    # pool/background surface) must NOT pull anyio/starlette/httpx.
    import subprocess
    import sys

    code = (
        "import sys, lancelot;"
        "lancelot.BackgroundTasks; lancelot.background;"
        "import lancelot.pool;"
        "bad=[m for m in ('anyio','starlette','httpx','fastapi') "
        "if m in sys.modules];"
        "print('LEAK' if bad else 'CLEAN', bad)"
    )
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True)
    assert out.stdout.strip().startswith("CLEAN"), out.stdout + out.stderr


def test_async_handler_still_inline_bytes_match_fastapi():
    # regression: the offload split must not change async-handler bytes
    import fastapi
    from fastapi.testclient import TestClient as FA
    import lancelot
    from lancelot.testclient import TestClient as LC

    def build(F):
        app = F.FastAPI() if hasattr(F, "FastAPI") else F.App()

        @app.get("/a")
        async def a():
            return {"async": True}

        return app

    with FA(build(fastapi)) as fa, LC(build(lancelot)) as lc:
        f, l = fa.get("/a"), lc.get("/a")
        assert l.status_code == f.status_code == 200
        assert l.content == f.content
```

- [ ] **Step 2: Run to verify it fails (or passes meaningfully)**

Run: `.venv/bin/python -m pytest tests/test_m1_p5_byte_parity.py -q`
Expected: the import-clean test FAILS if any P5 module top-imports a TEST-only dep; FIX by ensuring `pool.py`/`background.py` import only stdlib at module top (they do — verify, do not add deps).

- [ ] **Step 3: Make it green**

No production change expected (the modules are stdlib-only by construction). If the guard fails, move any offending import inside a function. Re-run until CLEAN.

- [ ] **Step 4: Commit**

```bash
LC=/Users/seventyleven/Desktop/Lancelot
git -C "$LC" add tests/test_m1_p5_byte_parity.py
.venv/bin/python -m pytest tests/ -q >/dev/null 2>&1
rc=$?; test $rc -eq 0 && git -C "$LC" commit -q -m "test(m1-p5): byte-parity consolidation + dependency-light import guard"
```

---

### Task 8: Conformance seed + honest RED ledger + clean-sweep + review + tag/push

**Files:**
- Create: `conformance/vendor/quarantine/test_p5_longtail.py` (RED ledger, excluded from the ratchet)
- Modify: `conformance/` seed if a P5 conformance case is added (only if it fits the existing harness; otherwise the byte-parity file is the oracle, as in P2-P4)

- [ ] **Step 1: Write the honest RED ledger**

```python
# conformance/vendor/quarantine/test_p5_longtail.py
"""M1-P5 LEDGER (quarantined, excluded from the ratchet) — the
genuinely-deferred blocking-pool/background tail:
  1. Rust 3-variant Empty/Mono/Pool runner (src/blocking/runner.rs) +
     crossbeam BOUNDED(10_000) + py.detach()-on-recv + adaptive
     single-flight spawn + idle-timeout death, AND src/background/pool.rs
     — these are the spec §3-69 Rust-pipeline unification (the
     conformance/TestClient path is the Python ASGI seam today; the
     Python pool here is the realization the byte oracle exercises).
  2. /metrics queue-depth / in-flight / rejected counters — that is
     U35 (M3 observability), not M1.
  3. Graceful-shutdown drain of in-flight blocking + background work on
     lifespan shutdown / SIGTERM (U34/U7, M3).
  4. Per-route / per-app pool sizing config surface (M1 ships a global
     `lancelot.pool.configure()` + `BackgroundPool(max_workers,...)`;
     the per-route knob rides U38/U19 settings).
Graduate into conformance/vendor/ as M3 + the §3-69 unification land."""
import pytest


def test_rust_blocking_runner_and_bg_pool_unification_S369():
    pytest.fail("spec §3-69: Rust src/blocking/runner.rs + "
                "src/background/pool.rs wired into the Rust hyper path")


def test_pool_metrics_queue_depth_U35_M3():
    pytest.fail("U35/M3: blocking_pool_* + background queue depth in "
                "/metrics")


def test_graceful_drain_on_shutdown_M3():
    pytest.fail("M3/U34: drain in-flight blocking+background work on "
                "lifespan shutdown / SIGTERM")
```

- [ ] **Step 2: Verify the ledger is RED and excluded from the ratchet**

Run:
```bash
.venv/bin/python -m pytest conformance/vendor/quarantine/test_p5_longtail.py -q   # 3 failed (expected)
make m1-conformance                                                                # still 11/11 = 100% (quarantine excluded)
```
Expected: 3 failed in the quarantine file; ratchet unaffected (floor 100).

- [ ] **Step 3: Full regression + Rust clean-sweep**

```bash
LC=/Users/seventyleven/Desktop/Lancelot
.venv/bin/python -m pytest tests/ -q                  # all green
make m1-conformance && make m1-spine && make m0       # 11/11=100, spine green, M0 7/7
.venv/bin/python -c "import lancelot"                  # import-clean
# Rust clean-sweep: P5 is pure-Python — confirm ZERO Rust delta + clippy
git -C "$LC" diff --stat m1-p4-green -- 'crates/**/*.rs' '**/Cargo.toml'   # empty
(cd "$LC" && export PATH="$HOME/.cargo/bin:$PATH" && cargo clippy --quiet --all-targets -- -D warnings)
```
Expected: tests green; conformance 11/11; M0 7/7; zero Rust delta; clippy rc=0.

- [ ] **Step 4: Dedicated bug/vuln/plan-alignment review agent**

Dispatch a fresh general-purpose review agent (same discipline as M1-P4):
scope = `lancelot/pool.py`, `lancelot/background.py`, `lancelot/asgi.py`,
`lancelot/di.py` deltas. It must: independently verify (a) no sync work
runs on the loop thread (probe `threading.get_ident`), (b) `PoolFull`/
`QueueFull` bounds are real (not unbounded), (c) byte parity vs real
FastAPI for sync handler / sync dep / BackgroundTasks, (d) the
ordered-wire-header invariant holds for the 503 (content-length then
content-type last), (e) no deadlock (offload from within an offloaded
thread; bg task that itself spawns), (f) the ledger is honest and
quarantine is ratchet-excluded, (g) the M0 dependency-light import
guard still holds. Verdict TAG-READY / NOT-READY with file:line fixes.
Fix any blocker via TDD; re-confirm.

- [ ] **Step 5: Tag + push + memory**

```bash
LC=/Users/seventyleven/Desktop/Lancelot
git -C "$LC" tag -a m1-p5-green -m "M1-P5 GREEN: bounded blocking pool (U32, fixes the FastAPI sync cliff) + BackgroundTasks byte-parity + detached background.spawn (U33); 503 Retry-After on overload; Rust runner/pool relocation ledgered §3-69"
git -C "$LC" push --follow-tags origin HEAD
```
Then update `~/.claude/projects/-Users-seventyleven-Desktop-Lancelot/memory/lancelot-m1-progress.md` (M1-P5 done; refresh the P6/P7 + §3-69 ledger).

---

## Self-Review

**1. Spec coverage:**
- U32 (blocking pool, bounded, GIL-friendly, wired into HTTP, 503+Retry-After on overload, sync classified at registration) → T1, T2, T3, T4. `is_async` is decided at compile time (`Plan.is_async`), so a sync fn never touches the loop — matches the WBS "classified at registration" requirement. The Rust `runner.rs` 3-variant/crossbeam realization is the §3-69 unification → ledgered (T8) — honest, not silently dropped.
- U33 (background task pool, detached, bounded, separate from request workers, `await_in_request` opt-in, response returns immediately) → T5 (FastAPI `BackgroundTasks` byte/timing parity) + T6 (`background.spawn` detached pool). `/metrics` queue depth is U35/M3 → ledgered.
- FastAPI parity (sync handler + sync dep offloaded; BackgroundTasks run-after-response order) → T2/T3/T5 byte-differential vs real fastapi 0.136.1.

**2. Placeholder scan:** none — every step has full code, exact paths, exact commands, expected output. No "add error handling"/"similar to"/TBD.

**3. Type consistency:** `BlockingPool.offload`/`get_pool`/`configure`/`PoolFull` (pool.py) used identically in asgi.py/di.py/background.py. `BackgroundTasks.add_task`/`__call__`, `BackgroundPool`/`QueueFull`/`spawn` names consistent across T5/T6/T8. `Plan.is_async` and `Plan.request_params` match the existing `di.py` definitions (verified against `di.py:30-37,85-86`). The `request_params` change preserves the bare-name path (M1-P4 `Request` injection) and adds only the tagged `("__bg__", name)` tuple — backward compatible, verified against `di.py:164-165,230-231`.

**Risk notes (carry into execution):**
- The 8×0.25s concurrency assertions use a generous `< 1.5s` ceiling to avoid CI flakiness; if a machine is slow, raise the ceiling — the property (not the exact wall-clock) is what matters (serialized would be ~2.0s).
- `test_pool_full_*` and `test_background_spawn_bounded_*` mutate global pools; both restore defaults in `finally` so suite order is irrelevant.
- Generator/async-gen yield-deps stay on the loop (unchanged) so the M1-P2 B4 `gen.throw` teardown semantics are preserved — only the plain-sync dep branch is offloaded.
