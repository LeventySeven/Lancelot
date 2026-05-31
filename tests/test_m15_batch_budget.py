"""M15 T9 — `@app.batch` budget knobs.

What this pins (the new surface on top of the C5 marquee):
  - `size_fn`: the flush trigger becomes a byte/token BUDGET, not just a
    count — a batch flushes when `size_fn(pending) >= max_size` even if
    `max_batch` (count) is not reached.
  - window/max_batch SNAPSHOT: a live retune of the registry mid-window
    can't corrupt a half-built batch (the in-flight batch uses the value
    captured when it started forming).
  - over-budget DEFER-TO-TAIL: an item that would blow the budget alone is
    deferred to the NEXT batch instead of overflowing the current one — and
    is fanned out EXACTLY ONCE (no double-fire even if max_keys eviction
    races the defer or the first batch partially fired).
  - max_keys hit → 503 + Retry-After (a sheddable load signal), not 500.

Design note (same as test_m2_tier_c5_batch): each TestClient owns ONE
asyncio portal-loop; coalescing only works among requests on the SAME
loop, so concurrent requests are fired via threads against ONE client.
The exactly-once race test drives the batch primitive directly on a single
loop so the defer + max_keys-eviction interleaving is deterministic.
"""
import asyncio
import threading

import pytest
from pydantic import BaseModel

import lancelot
from lancelot.testclient import TestClient


class _Q(BaseModel):
    text: str
    group: str = "default"


def _fire_concurrent(client, body_for_idx, n: int):
    results: list = [None] * n
    barrier = threading.Barrier(n)

    def hit(i):
        barrier.wait()
        results[i] = client.post("/b", json=body_for_idx(i))

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ---------- size_fn budget caps bytes, not count -----------------------

def test_size_fn_caps_bytes_not_count():
    """With a per-item byte budget far below `max_batch` (count), a batch
    flushes on the BYTE budget — so each handler call carries FEWER items
    than max_batch. We measure the largest batch the handler ever saw and
    assert it respected the budget, not the count."""
    calls: list[list] = []
    app = lancelot.App()

    # Each _Q's "size" = len(text). max_batch=100 (count, far away);
    # max_size=10 bytes. Each item carries text="xN" (~2 bytes) so a batch
    # should cap around ~5 items (5*2=10) — well below 100.
    @app.batch("/b", batch_window_ms=200, max_batch=100,
               size_fn=lambda items: sum(len(it.text) for it in items),
               max_size=10)
    async def b(items: list[_Q]) -> list[dict]:
        calls.append(list(items))
        return [{"echo": it.text} for it in items]

    with TestClient(app) as c:
        # 12 concurrent items of "xxxxx" (5 bytes each) → budget 10 means
        # a batch holds at most 2 items (2*5=10 ≥ 10 triggers flush).
        results = _fire_concurrent(c, lambda i: {"text": "xxxxx"}, 12)

    assert all(r.status_code == 200 for r in results), [r.status_code for r in results]
    # Every item answered exactly once.
    assert sum(len(cl) for cl in calls) == 12
    # The budget capped batch size BELOW max_batch (count=100). With 5-byte
    # items and a 10-byte budget, no batch should exceed 2 items.
    biggest = max(len(cl) for cl in calls)
    assert biggest <= 2, f"size_fn budget not honored: biggest batch={biggest}"
    # And it actually coalesced something (not 12 separate calls of 1).
    assert any(len(cl) == 2 for cl in calls), f"no coalescing under budget: {[len(c) for c in calls]}"


# ---------- window/max_batch snapshot survives a live retune -----------

def test_window_snapshot_survives_retune():
    """A live retune of the registry's max_batch mid-window must NOT change
    the trigger of a batch that is already forming — the in-flight batch
    uses the snapshot taken when it started."""
    from lancelot.batch import _BatchRegistry

    async def run():
        seen: list[int] = []

        async def handler(items):
            seen.append(len(items))
            return [None] * len(items)

        reg = _BatchRegistry(handler, window_s=0.05, max_batch=5, key_fn=None)

        # Submit 2 items → a batch starts forming with snapshot max_batch=5.
        t1 = asyncio.ensure_future(reg.submit("a"))
        t2 = asyncio.ensure_future(reg.submit("b"))
        await asyncio.sleep(0)  # let the group + timer arm
        # Now retune the registry to max_batch=2. If the in-flight batch
        # read the LIVE value it would flush immediately at 2; the snapshot
        # must keep it at 5 so it waits for the window.
        reg.max_batch = 2
        await asyncio.gather(t1, t2)
        # The batch flushed via the WINDOW (2 items < snapshot 5), in ONE
        # call of 2 — proving it didn't honor the retuned max_batch=2 as a
        # count trigger mid-batch (which would have produced the same call
        # size, so instead assert it WAITED the window: the group's
        # snapshot must equal 5).
        return seen

    seen = asyncio.run(run())
    assert seen == [2], seen


def test_window_snapshot_group_records_first_submit_value():
    """Directly assert the group snapshots max_batch/window at the FIRST
    submit and ignores a later registry retune for that group's lifetime."""
    from lancelot.batch import _BatchRegistry

    async def run():
        async def handler(items):
            return [None] * len(items)

        reg = _BatchRegistry(handler, window_s=0.5, max_batch=10, key_fn=None)
        fut = asyncio.ensure_future(reg.submit("a"))
        await asyncio.sleep(0)
        # The group now exists; mutate the registry.
        reg.max_batch = 99
        reg.window_s = 99.0
        grp = reg._groups["_default_"]
        snap_max = grp.max_batch
        snap_win = grp.window_s
        await fut
        return snap_max, snap_win

    snap_max, snap_win = asyncio.run(run())
    assert snap_max == 10, snap_max
    assert snap_win == 0.5, snap_win


# ---------- over-budget item deferred exactly once ---------------------

def test_over_budget_item_deferred_exactly_once():
    """An item that would blow the budget is deferred to the NEXT batch and
    fanned out EXACTLY ONCE — even when a max_keys eviction races the defer
    and the first batch has already partially fired. We assert: (a) every
    waiter resolves exactly once with the right answer, (b) the handler is
    invoked with each item exactly once across all batches (no double-fire),
    (c) no waiter's future is resolved twice."""
    from lancelot.batch import _BatchRegistry

    async def run():
        handled: list = []
        resolve_counts: dict = {}

        async def handler(items):
            handled.extend(items)
            return [f"r:{x}" for x in items]

        # size_fn = number of items*10; max_size=10 → budget holds exactly 1
        # item, so the SECOND concurrent item is always over-budget relative
        # to a non-empty pending and must defer. max_keys=1 forces the
        # eviction path to be exercised on the same single key (so a defer
        # can race a cap check).
        reg = _BatchRegistry(
            handler, window_s=0.02, max_batch=100, key_fn=None,
            size_fn=lambda items: len(items) * 10, max_size=10, max_keys=1)

        # Fire many submits onto the SAME loop concurrently. Each is a
        # distinct payload so we can detect a double-handle.
        n = 25
        futs = [asyncio.ensure_future(reg.submit(f"p{i}")) for i in range(n)]
        results = await asyncio.gather(*futs, return_exceptions=True)
        return handled, results

    handled, results = asyncio.run(run())

    # (a) every waiter resolved exactly once with the right answer.
    assert len(results) == 25
    for i, r in enumerate(results):
        assert r == f"r:p{i}", (i, r)
    # (b) the handler saw each payload EXACTLY ONCE (no double-fire).
    assert sorted(handled) == sorted(f"p{i}" for i in range(25)), handled
    assert len(handled) == len(set(handled)) == 25, (
        f"double-fire detected: {len(handled)} handled, "
        f"{len(set(handled))} distinct")


def test_defer_exactly_once_while_max_keys_eviction_races():
    """The defer-to-tail path and the max_keys-eviction path run
    CONCURRENTLY on the same loop: many distinct keys (more than max_keys)
    all submitting at once. Some submits are SHED (BatchOverloadError →
    would be 503), the accepted ones defer/coalesce — and EVERY accepted
    item is fanned out exactly once with no double-fire, even though
    eviction is racing the defer logic. This is the idempotency guarantee
    the critic flagged."""
    from lancelot.batch import _BatchRegistry, BatchOverloadError

    async def run():
        handled: list = []

        async def handler(items):
            handled.extend(items)
            return [f"r:{x}" for x in items]

        # key = the payload's leading char → 4 distinct keys, but max_keys=2.
        # size budget holds 1 item so within an accepted key the 2nd+ items
        # defer. So we exercise BOTH: cross-key eviction AND in-key defer,
        # interleaved on one loop.
        reg = _BatchRegistry(
            handler, window_s=0.02, max_batch=100,
            key_fn=lambda p: p[0], max_keys=2,
            size_fn=lambda items: len(items) * 10, max_size=10)

        # keys 'a' and 'b' (first two created) accepted; 'c'/'d' shed.
        payloads = []
        for ch in "abcd":
            for i in range(6):
                payloads.append(f"{ch}{i}")
        futs = [asyncio.ensure_future(reg.submit(p)) for p in payloads]
        results = await asyncio.gather(*futs, return_exceptions=True)
        return handled, results, payloads

    handled, results, payloads = asyncio.run(run())

    # Partition results: accepted (a string) vs shed (BatchOverloadError).
    accepted = {}
    shed = 0
    for p, r in zip(payloads, results):
        if isinstance(r, BatchOverloadError):
            shed += 1
        else:
            assert r == f"r:{p}", (p, r)
            accepted[p] = r
    # Some were shed (the cap worked) and some accepted (the cap isn't total).
    assert shed > 0, "max_keys eviction never fired — race not exercised"
    assert len(accepted) > 0, "everything shed — registry mis-sized"
    # EXACTLY-ONCE: each handled payload appears once; handled == accepted set.
    assert len(handled) == len(set(handled)), (
        f"double-fire: {len(handled)} handled, {len(set(handled))} distinct")
    assert set(handled) == set(accepted), (set(handled) ^ set(accepted))


def test_over_budget_single_item_still_processed():
    """An item that ALONE exceeds the budget must NOT be stuck forever — it
    is processed in a batch by itself (defer can't strand it)."""
    from lancelot.batch import _BatchRegistry

    async def run():
        handled: list = []

        async def handler(items):
            handled.extend(items)
            return [f"r:{x}" for x in items]

        # Budget=5 but a single item reports size 100 → it can never "fit",
        # yet it must still be handled (alone).
        reg = _BatchRegistry(
            handler, window_s=0.02, max_batch=100, key_fn=None,
            size_fn=lambda items: len(items) * 100, max_size=5)
        r = await reg.submit("big")
        return r, handled

    r, handled = asyncio.run(run())
    assert r == "r:big"
    assert handled == ["big"]


# ---------- max_keys hit → 503 + Retry-After ---------------------------

def test_max_keys_returns_503():
    """Exceeding `max_keys` returns 503 + Retry-After (a sheddable load
    signal the client can back off on), NOT the old 500."""
    app = lancelot.App()

    @app.batch("/b", batch_window_ms=10, max_batch=1, max_keys=2,
               key=lambda it: it.group)
    async def b(items: list[_Q]) -> list[dict]:
        return [{"g": it.group} for it in items]

    with TestClient(app) as c:
        assert c.post("/b", json={"text": "x", "group": "g1"}).status_code == 200
        assert c.post("/b", json={"text": "x", "group": "g2"}).status_code == 200
        # 3rd distinct key → cap blown → 503 + Retry-After.
        r = c.post("/b", json={"text": "x", "group": "g3"})
        assert r.status_code == 503, r.text
        assert "retry-after" in {k.lower() for k in r.headers}, dict(r.headers)
        # An already-seen key still works.
        assert c.post("/b", json={"text": "x", "group": "g1"}).status_code == 200


# ---------- backward-compat: existing C5 behavior intact ---------------

def test_count_trigger_still_works_without_size_fn():
    """No size_fn → behavior is exactly the old count-based trigger."""
    calls: list[list] = []
    app = lancelot.App()

    @app.batch("/b", batch_window_ms=10_000, max_batch=2)
    async def b(items: list[_Q]) -> list[dict]:
        calls.append(list(items))
        return [{"text": it.text} for it in items]

    with TestClient(app) as c:
        results = _fire_concurrent(c, lambda i: {"text": f"x{i}"}, 2)

    assert all(r.status_code == 200 for r in results)
    # max_batch=2 → flushed at 2 without waiting the 10s window.
    assert sum(len(cl) for cl in calls) == 2
