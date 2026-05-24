"""M7 T4.6 — per-tenant MemoryPool with named-consumer OOM (DataFusion
pattern DATAFUSION.md:2058-2265).

The framework provides the primitive; users wire it into handler code
that does big allocs (embedding batches, audio buffers, etc.).
"""
import asyncio

import pytest

from lancelot.memory_pool import (
    MemoryPool, MemoryReservation, ResourcesExhausted,
)


def test_t4_6_pool_construction_requires_positive_budget():
    with pytest.raises(ValueError):
        MemoryPool(0)
    with pytest.raises(ValueError):
        MemoryPool(-1)


def test_t4_6_basic_reserve_and_release():
    pool = MemoryPool(1000)
    assert pool.used_bytes == 0
    assert pool.available_bytes == 1000

    res = pool.reserve("route-a", 400)
    assert pool.used_bytes == 400
    assert pool.available_bytes == 600
    assert res.bytes == 400

    res.release()
    assert pool.used_bytes == 0
    assert pool.available_bytes == 1000


def test_t4_6_release_is_idempotent():
    pool = MemoryPool(1000)
    res = pool.reserve("x", 100)
    res.release()
    res.release()  # second call → no-op
    assert pool.used_bytes == 0


def test_t4_6_sync_context_manager_releases_on_exit():
    pool = MemoryPool(1000)
    with pool.reserve("x", 200):
        assert pool.used_bytes == 200
    assert pool.used_bytes == 0


def test_t4_6_async_context_manager():
    pool = MemoryPool(1000)

    async def run():
        async with pool.reserve("x", 300):
            assert pool.used_bytes == 300
        assert pool.used_bytes == 0

    asyncio.run(run())


def test_t4_6_oom_names_top_consumer():
    pool = MemoryPool(1000)
    # IMPORTANT: bind the reservations to local names — otherwise the
    # MemoryReservation is GC'd immediately, __del__ releases the bytes,
    # and the test wouldn't observe over-budget. This is the same
    # pattern users will follow (the `with` block; or explicit `r = ...`).
    big = pool.reserve("route-big", 600)
    small = pool.reserve("route-small", 300)
    try:
        with pytest.raises(ResourcesExhausted) as exc:
            pool.reserve("route-mid", 200)

        # Message names the TOP consumer.
        assert "route-big" in str(exc.value)
        # And carries structured fields.
        assert exc.value.top_consumer == "route-big"
        assert exc.value.top_consumer_bytes == 600
        assert exc.value.requested_bytes == 200
        assert exc.value.available_bytes == 100
    finally:
        big.release()
        small.release()


def test_t4_6_human_readable_oom_message():
    """Error message uses human-readable byte sizes (380MB, 8GB)."""
    pool = MemoryPool(500 * 1024**2)  # 500 MB
    big = pool.reserve("embed:acme", 380 * 1024**2)  # 380 MB
    try:
        with pytest.raises(ResourcesExhausted) as exc:
            pool.reserve("embed:other", 200 * 1024**2)  # too big
        msg = str(exc.value)
        assert "380.0MB" in msg
        assert "embed:acme" in msg
    finally:
        big.release()


def test_t4_6_consumers_snapshot():
    pool = MemoryPool(1000)
    a = pool.reserve("a", 100)
    b = pool.reserve("b", 200)
    try:
        snap = pool.consumers()
        assert snap == {"a": 100, "b": 200}
    finally:
        a.release()
        b.release()


def test_t4_6_zero_byte_reserve_is_trivial():
    pool = MemoryPool(1000)
    res = pool.reserve("x", 0)
    assert res.bytes == 0
    assert pool.used_bytes == 0
    res.release()


def test_t4_6_negative_bytes_rejected():
    pool = MemoryPool(1000)
    with pytest.raises(ValueError):
        pool.reserve("x", -5)


def test_t4_6_multiple_reservations_same_name_aggregate():
    """Two reservations for the same name accumulate in the consumers map."""
    pool = MemoryPool(1000)
    r1 = pool.reserve("route-x", 100)
    r2 = pool.reserve("route-x", 250)
    assert pool.consumers() == {"route-x": 350}
    r1.release()
    assert pool.consumers() == {"route-x": 250}
    r2.release()
    assert pool.consumers() == {}


def test_t4_6_pool_recovers_after_release_unblocks_oom():
    """Releasing a big consumer should allow a previously-OOM'd reserve
    to succeed."""
    pool = MemoryPool(1000)
    big = pool.reserve("big", 800)
    try:
        with pytest.raises(ResourcesExhausted):
            pool.reserve("med", 300)
    finally:
        big.release()
    # Release the big consumer; med can now fit.
    med = pool.reserve("med", 300)
    assert pool.used_bytes == 300
    med.release()
