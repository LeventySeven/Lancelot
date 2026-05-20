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
        # Tier-B.B3: copy the current asyncio context (ContextVars set by
        # the ASGI seam — request_id etc.) into the worker thread so sync
        # handlers and yield-deps see the same per-request context. By
        # default `run_in_executor` runs `fn` in an EMPTY context; we use
        # `contextvars.copy_context().run(...)` to propagate.
        import contextvars as _cv

        ctx = _cv.copy_context()
        try:
            return await loop.run_in_executor(
                self._ex, lambda: ctx.run(fn, *args, **kwargs))
        finally:
            with self._lock:
                self._inflight -= 1

    async def run_backpressured(self, fn, /, *args, **kwargs):
        """Post-response background path: queue on the executor WITHOUT the
        inflight gate, so it BACKPRESSURES (like Starlette run_in_threadpool
        / the anyio limiter) instead of raising PoolFull. The inflight 503
        gate is for REQUEST admission only — rejecting already-accepted
        background work (and unwinding a SUCCESSFUL request's yield-deps
        with a synthetic PoolFull) is a data-integrity bug and diverges
        from FastAPI (review blocker #1). A task that raises on its own
        still propagates — that IS FastAPI's behavior."""
        loop = asyncio.get_running_loop()
        import contextvars as _cv

        ctx = _cv.copy_context()
        return await loop.run_in_executor(
            self._ex, lambda: ctx.run(fn, *args, **kwargs))

    def shutdown(self):
        self._ex.shutdown(wait=False, cancel_futures=True)


_DEFAULT: "BlockingPool | None" = None
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
