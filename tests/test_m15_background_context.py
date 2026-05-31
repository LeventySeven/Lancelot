"""M15 T3 (#12) — detached background.spawn propagates request context.

`pool.offload` and `pool.run_backpressured` both copy the caller's
contextvars into the worker thread (so sync handlers / yield-deps see the
per-request request_id / trace_id / tenant_id). The detached BackgroundPool
used by `background.spawn` did NOT — it submitted the body to a
ThreadPoolExecutor with no `contextvars.copy_context()`, so a spawned job ran
in an EMPTY context and read the `'-'` / `0.0` sentinels. That asymmetry
(offload propagates, spawn doesn't) is the bug. Lancelot owns these
ContextVars; propagating them into its own detached pool is byte-path.
"""
import threading

import pytest

import lancelot.background as bg
from lancelot.context import agent_id, request_id, tenant_id, trace_id


def _spawn_and_capture(setters: dict, getters: list[str]):
    """Set the given ContextVars, spawn a fn that reads `getters`, return the
    values it observed (waiting on a threading.Event so the detached job has
    run). Tokens are reset afterward."""
    holder: dict = {}
    done = threading.Event()
    tokens = [(var, var.set(val)) for var, val in setters.items()]

    name_to_var = {"request_id": request_id, "trace_id": trace_id,
                   "tenant_id": tenant_id, "agent_id": agent_id}

    def job():
        try:
            for n in getters:
                holder[n] = name_to_var[n].get()
        finally:
            done.set()

    try:
        bg.spawn(job)
        assert done.wait(timeout=5.0), "spawned job did not run in time"
    finally:
        for var, tok in tokens:
            var.reset(tok)
    return holder


def test_spawn_propagates_request_id():
    got = _spawn_and_capture({request_id: "rid-abc-123"}, ["request_id"])
    # Before the fix the detached job runs in an empty context → sees "-".
    assert got["request_id"] == "rid-abc-123"


def test_spawn_propagates_trace_and_tenant():
    got = _spawn_and_capture(
        {trace_id: "0af7651916cd43dd8448eb211c80319c",
         tenant_id: "acme",
         agent_id: "agent-7"},
        ["trace_id", "tenant_id", "agent_id"])
    assert got["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
    assert got["tenant_id"] == "acme"
    assert got["agent_id"] == "agent-7"


def test_spawn_still_bounded():
    """Propagation must not regress the bounded-queue ceiling: spawning past
    max_workers + max_queue still raises QueueFull."""
    pool = bg.BackgroundPool(max_workers=1, max_queue=1)  # ceiling = 2
    release = threading.Event()
    started = threading.Event()

    def blocker():
        started.set()
        release.wait(timeout=5.0)

    # 1 running (occupies the worker) + 1 queued = ceiling reached.
    pool.spawn(blocker)
    assert started.wait(timeout=5.0)
    pool.spawn(blocker)  # queued — fills the bounded queue
    with pytest.raises(bg.QueueFull):
        pool.spawn(blocker)  # over the ceiling → refused
    release.set()
    pool.shutdown()


def test_spawn_context_does_not_leak_back_to_caller():
    """The spawned job mutating a ContextVar inside its copied context must
    NOT bleed back into the caller's context (copy_context isolation)."""
    tok = request_id.set("caller-rid")
    done = threading.Event()

    def job():
        try:
            request_id.set("job-rid")  # mutate inside the copied context
        finally:
            done.set()

    try:
        bg.spawn(job)
        assert done.wait(timeout=5.0)
        # The caller still sees its own value — no cross-context bleed.
        assert request_id.get() == "caller-rid"
    finally:
        request_id.reset(tok)
