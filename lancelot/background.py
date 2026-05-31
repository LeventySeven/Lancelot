"""BackgroundTasks (FastAPI byte/timing parity — tasks run AFTER the
response is sent) + the detached background pool (WBS U33). Sync tasks
run on the M1-P5 blocking pool so they never pin the reactor; FastAPI
runs them via run_in_threadpool too. Stdlib-only (runtime dep =
pydantic)."""
import contextvars
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor


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
                # BACKPRESSURE (not the 503 inflight gate): post-send work
                # is already accepted — Starlette run_in_threadpool queues
                # it, never rejects. Using offload() here would raise a
                # synthetic PoolFull into the (successful) request's
                # solve_async scope and spuriously roll back yield-deps.
                await get_pool().run_backpressured(func, *args, **kwargs)


class QueueFull(Exception):
    """Detached background queue exhausted (WBS U33 bounded queue)."""


class BackgroundPool:
    """Detached pool SEPARATE from request workers (WBS U33): a request
    returns immediately; the job outlives it. Bounded → QueueFull rather
    than unbounded growth. `_depth` counts submitted-but-unfinished tasks;
    the ceiling is `max_workers + max_queue` (running occupy workers,
    the rest wait in the bounded queue). The Rust src/background/pool.rs
    realization is the spec §3-69 unification (ledgered RED)."""

    def __init__(self, max_workers: int = 8, max_queue: int = 10_000):
        self._ex = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="lancelot-bg")
        self._ceiling = max_workers + max_queue
        self._depth = 0
        self._lock = threading.Lock()

    def spawn(self, fn, /, *args, **kwargs):
        with self._lock:
            if self._depth >= self._ceiling:
                raise QueueFull()
            self._depth += 1

        # M15 T3 (#12): snapshot the CALLER's context NOW (synchronously, in
        # the request's context) so the detached job sees the per-request
        # request_id / trace_id / tenant_id / agent_id — matching pool.offload
        # / run_backpressured (pool.py), which already copy_context(). Without
        # this the job runs in an empty context and reads the "-"/0.0
        # sentinels. copy_context() also isolates the job's own ContextVar
        # writes from the caller (no bleed-back).
        ctx = contextvars.copy_context()

        def _run():
            try:
                ctx.run(fn, *args, **kwargs)
            finally:
                with self._lock:
                    self._depth -= 1

        try:
            self._ex.submit(_run)
        except RuntimeError:  # submit after shutdown → don't leak _depth
            with self._lock:
                self._depth -= 1
            raise

    def shutdown(self):
        self._ex.shutdown(wait=False, cancel_futures=True)


_POOL: "BackgroundPool | None" = None
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
