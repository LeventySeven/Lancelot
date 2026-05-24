"""M7 T4.6 — per-tenant fair MemoryPool with named-consumer OOM (DataFusion
pattern, DATAFUSION.md:2058-2265).

The framework primitive: a bounded per-process memory budget, tracked by
consumer name (typically `route=/v1/embed tenant=acme`). Out-of-budget
raises ResourcesExhausted with the TOP consumer named in the error
message — so operators get "OOM caused by route=/v1/embed (tenant=acme,
380MB)" instead of "out of memory" with no clue who's eating the budget.

This is a COOPERATIVE pool — handlers explicitly reserve before doing
the big alloc (e.g. loading an embedding batch into RAM). The framework
doesn't intercept Python's malloc; it intercepts the explicit reserve
call. Pairs with T2.4 cardinality cap on the metric side.

Usage:
    pool = MemoryPool(total_bytes=8 * 1024**3)  # 8 GB
    async with pool.reserve("embed:acme", 380 * 1024**2):
        # ... do the big alloc; bytes counted ...
    # on exit: budget restored

The pattern matches DataFusion's `TrackConsumersPool` exactly.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager


class ResourcesExhausted(Exception):
    """Raised by MemoryPool.reserve when the budget is exceeded.

    Carries `top_consumer` (the name with the largest current reservation)
    + `top_consumer_bytes` so operator dashboards can surface "who's
    eating the pool right now"."""

    def __init__(self, msg: str, *, top_consumer: str | None = None,
                 top_consumer_bytes: int = 0,
                 requested_bytes: int = 0,
                 available_bytes: int = 0):
        super().__init__(msg)
        self.top_consumer = top_consumer
        self.top_consumer_bytes = top_consumer_bytes
        self.requested_bytes = requested_bytes
        self.available_bytes = available_bytes


class MemoryReservation:
    """RAII handle for a single reservation. Returned by
    `MemoryPool.reserve(...)`. On exit (sync `with` or async `async with`)
    or `release()`, the bytes go back to the pool."""

    __slots__ = ("_pool", "_name", "_bytes", "_released")

    def __init__(self, pool: "MemoryPool", name: str, n_bytes: int):
        self._pool = pool
        self._name = name
        self._bytes = n_bytes
        self._released = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def bytes(self) -> int:
        return self._bytes

    def release(self) -> None:
        """Return the bytes to the pool. Idempotent."""
        if self._released:
            return
        self._released = True
        self._pool._release(self._name, self._bytes)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.release()

    def __del__(self):
        # Best-effort: if a reservation is leaked (no `with`/release),
        # still account it back when GC'd. Don't raise from __del__.
        try:
            self.release()
        except Exception:  # noqa: BLE001
            pass


class MemoryPool:
    """Bounded process-wide memory budget. Consumers reserve under
    named keys (typically `route:tenant`); over-budget raises
    ResourcesExhausted naming the top consumer.

    Not async-safe by itself — but the inner lock is short and
    contention should be rare (operators set the pool bigger than
    expected concurrent demand)."""

    def __init__(self, total_bytes: int):
        if total_bytes <= 0:
            raise ValueError("total_bytes must be positive")
        self._total = total_bytes
        self._used = 0
        self._consumers: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def total_bytes(self) -> int:
        return self._total

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._used

    @property
    def available_bytes(self) -> int:
        with self._lock:
            return max(0, self._total - self._used)

    def consumers(self) -> dict[str, int]:
        """Snapshot of (name → bytes) for observability."""
        with self._lock:
            return dict(self._consumers)

    def reserve(self, name: str, n_bytes: int) -> MemoryReservation:
        """Reserve `n_bytes` for consumer `name`. Returns a
        MemoryReservation usable as a (sync OR async) context manager.
        Raises ResourcesExhausted on over-budget — error names the top
        current consumer so operators see who's eating the pool.

        Args:
            name: consumer identifier. Convention: `route:tenant` or
                just `route` for global routes. Cardinality-control is
                the caller's job (mirror the T2.4 tenant cap).
            n_bytes: bytes to reserve. Must be ≥ 0.
        """
        if n_bytes < 0:
            raise ValueError("n_bytes must be non-negative")
        if n_bytes == 0:
            # Trivial reservation; succeeds without locking.
            return MemoryReservation(self, name, 0)
        with self._lock:
            new_used = self._used + n_bytes
            if new_used > self._total:
                # Build the named-consumer error message.
                top_name, top_bytes = self._top_consumer_locked()
                avail = self._total - self._used
                msg = (
                    f"MemoryPool exhausted: requested {_human(n_bytes)} for "
                    f"'{name}', only {_human(avail)} available. Top consumer: "
                    f"'{top_name or '<none>'}' holding {_human(top_bytes)}."
                )
                raise ResourcesExhausted(
                    msg, top_consumer=top_name,
                    top_consumer_bytes=top_bytes,
                    requested_bytes=n_bytes,
                    available_bytes=avail)
            self._used = new_used
            self._consumers[name] = self._consumers.get(name, 0) + n_bytes
        return MemoryReservation(self, name, n_bytes)

    def _release(self, name: str, n_bytes: int) -> None:
        if n_bytes <= 0:
            return
        with self._lock:
            self._used = max(0, self._used - n_bytes)
            cur = self._consumers.get(name, 0)
            cur -= n_bytes
            if cur <= 0:
                self._consumers.pop(name, None)
            else:
                self._consumers[name] = cur

    def _top_consumer_locked(self) -> tuple[str | None, int]:
        if not self._consumers:
            return (None, 0)
        # max(items by value) — small dict, no need for a heap.
        name, b = max(self._consumers.items(), key=lambda kv: kv[1])
        return (name, b)


def _human(n: int) -> str:
    """Pretty-print byte count in error messages: 380.0MB, 8.0GB."""
    if n < 1024:
        return f"{n}B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f}{unit}"
    return f"{n:.1f}PB"


__all__ = ["MemoryPool", "MemoryReservation", "ResourcesExhausted"]
