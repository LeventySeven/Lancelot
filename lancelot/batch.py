"""@app.batch — prefix-aware request grouping (Tier-C.C5, marquee).

Many AI workloads spend money on calls that COULD be batched: the same
system prompt + N user messages can be one upstream LLM call instead of N.
FastAPI doesn't help — every request goes to its own handler. Lancelot
gives the user a decorator that groups concurrent HTTP requests by a
caller-defined key, waits a short window for more, then invokes the user
handler ONCE with the full batch.

What this is NOT: an LLM client. The user writes the batch handler; we
only do the grouping + fanout. (Cardinal rule: AI logic stays user-side.)

Surface:
    @app.batch("/chat", batch_window_ms=50, max_batch=10,
               key=lambda item: item.system_prompt)
    async def chat(items: list[ChatRequest]) -> list[ChatResponse]:
        # One call to OpenAI/Anthropic/local-vLLM with all items
        return await call_upstream(items)

The HTTP route accepts ONE `ChatRequest` per request; the handler sees
the full `list[ChatRequest]` after the window closes (or `max_batch`
fills first). The framework fans the `list[ChatResponse]` back to each
waiting client by index.

Error semantics: if the user handler raises, every waiter in that batch
sees the same exception (no half-success). If the handler returns the
wrong-length list, every waiter sees `RuntimeError` (contract failure)."""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable


class BatchOverloadError(Exception):
    """Raised when a batch registry sheds a NEW arrival. Two distinct
    saturations both map here (the ASGI seam → a sheddable 503 +
    Retry-After, a load signal the client backs off on, not a 500 — both
    are transient back-pressure, not a server bug):

      - the `max_keys` cap (too many distinct key-values) — `shed_reason`
        `"batch-keys"`;
      - the `max_concurrent_batches` compute cap is saturated (T1.5) —
        `shed_reason` `"batch-overload"`.

    `retry_after` is the integer seconds for the Retry-After header.
    `shed_reason` (T3.4) names WHICH cap fired so the operator can tell a
    key-explosion from a compute pile-up at the `X-Lancelot-Shed-Reason`
    seam — without leaking the internal limit number (info-disclosure)."""

    def __init__(self, message: str, *, retry_after: int = 1,
                 shed_reason: str = "batch-overload"):
        super().__init__(message)
        self.retry_after = retry_after
        self.shed_reason = shed_reason


class BatchItemTimeout(Exception):
    """Raised to a SINGLE waiter whose item aged past `request_timeout_ms`
    before its batch ran (T1.4). The ASGI seam maps it to 504 Gateway
    Timeout — a per-request deadline, not whole-server backpressure (that's
    BatchOverloadError -> 503). We set the waiter's future to THIS exception
    rather than silently dropping the item: a dropped-but-never-resolved
    future hangs the client forever — the exact M4 failure class this whole
    completion bundle is tested against (the awaiting handler path)."""


@dataclass
class _BatchGroup:
    """One bucket. There's a separate group per `key`-value the user
    function partitions on. Mutex-protected so submissions and the
    flush-timer don't race on `pending`.

    `window_s` / `max_batch` are SNAPSHOTTED into the group at
    construction (first submit for this key) — a later live retune of the
    parent registry can't corrupt a half-built batch (M15 T9). The group
    keeps using its captured values for its whole lifetime.

    `size_fn` (optional, M15 T9): when set, the flush trigger is a
    byte/token BUDGET — a batch flushes when `size_fn(pending) >=
    max_size`, in ADDITION to the `max_batch` count trigger. An item that
    would push a non-empty batch over the budget is DEFERRED to the next
    batch (the current pending flushes first, the over-budget item starts
    a fresh one). A single item that exceeds the budget alone is still
    processed (in a batch by itself) — defer never strands it."""
    handler: Callable[..., Any]
    window_s: float
    max_batch: int
    size_fn: Callable[[list], int] | None = None
    max_size: int = 0
    request_timeout_s: float = 0.0  # T1.4: per-item queue-age deadline (0=off)
    dedup_key: Callable[..., Any] | None = None  # T1.1: identical-key collapse (off)
    # T1.3: shared-across-the-registry pipeline limiter. `sema` bounds the
    # number of handler awaits that may run AT ONCE (None = unbounded =
    # today's behavior); `inflight` is the registry's running-batch counter
    # (a 1-element list so every group of one registry mutates the SAME int —
    # read by the T1.5 submit-time shed). Both default to "off".
    sema: "asyncio.Semaphore | None" = None
    inflight: list | None = None  # [int]; shared registry counter
    pending: list = field(default_factory=list)  # (payload, [Future,...], enq_monotonic)
    timer_task: asyncio.Task | None = None
    lock: asyncio.Lock | None = None

    def __post_init__(self):
        # Eager lock so two concurrent first-submissions can't each
        # create their own Lock and break mutual exclusion. Python 3.10+
        # allows asyncio.Lock() construction without a running loop.
        if self.lock is None:
            self.lock = asyncio.Lock()

    def _over_budget_with(self, payload) -> bool:
        """Would adding `payload` push a NON-EMPTY pending over the byte
        budget? (An empty pending is never over-budget — a lone oversized
        item must still be processed.)"""
        if self.size_fn is None or not self.pending:
            return False
        prospective = [e[0] for e in self.pending] + [payload]
        return self.size_fn(prospective) > self.max_size

    def _budget_full(self) -> bool:
        """Has the current pending REACHED the byte budget (flush trigger)?"""
        if self.size_fn is None or not self.pending:
            return False
        return self.size_fn([e[0] for e in self.pending]) >= self.max_size

    async def submit(self, payload):
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        enq = time.monotonic()  # T1.4: queue-age clock for request_timeout
        # `deferred_batch` is a snapshot of a pending list that this submit
        # decided to flush BEFORE appending its own item (the defer-to-tail
        # path). It's run OUTSIDE the lock, exactly once — the swapped-out
        # list is unreachable to any other task, so there's no double-fire.
        deferred_batch = None
        flush_now = False
        async with self.lock:
            # T1.1 dedup (opt-in): if an identical key is already pending in
            # THIS window, attach this waiter to that entry so ONE upstream
            # call serves every identical request (retry storms / identical
            # polls / repeated-prompt RAG). dedup_key=None → every item is its
            # own entry (today's behavior). Linear scan; pending ≤ max_batch.
            matched = False
            if self.dedup_key is not None and self.pending:
                dk = self.dedup_key(payload)
                for entry in self.pending:
                    if self.dedup_key(entry[0]) == dk:
                        entry[1].append(fut)
                        matched = True
                        break
            if not matched:
                # Defer-to-tail: if this NEW payload would blow the budget for
                # the current (non-empty) pending, flush what's already there
                # first, then this item starts a fresh batch. Atomic list-swap
                # under the lock = exactly-once fanout of the deferred items.
                if self._over_budget_with(payload):
                    deferred_batch = self.pending
                    self.pending = []
                    # The timer (if any) was armed for the now-deferred batch;
                    # leave it — it'll fire on the fresh pending or find it
                    # flushed-empty (→ early return). Re-armed below if needed.
                self.pending.append((payload, [fut], enq))
            if len(self.pending) >= self.max_batch or self._budget_full():
                flush_now = True
            elif self.timer_task is None or self.timer_task.done():
                # First in this window (or the prior timer already fired):
                # arm a fresh timer for the current pending.
                self.timer_task = asyncio.ensure_future(self._wait_then_flush())
        # Run the deferred batch first (it formed earlier), then ours. Both
        # are no-ops if empty. Neither holds the lock here.
        if deferred_batch:
            await self._run_batch(deferred_batch)
        if flush_now:
            await self._flush()
        return await fut

    async def _wait_then_flush(self):
        try:
            await asyncio.sleep(self.window_s)
        except asyncio.CancelledError:
            return
        await self._flush()

    async def _flush(self):
        async with self.lock:
            batch = self.pending
            self.pending = []
            timer = self.timer_task
            self.timer_task = None
        # Cancel a still-pending timer ONLY when flushing from a different task
        # (the max_batch / flush_now path) — never the timer's OWN task. The
        # timer-window flush runs INSIDE `_wait_then_flush`; cancelling
        # `self.timer_task` there cancels the current task, and the
        # CancelledError lands on the next `await` (the user handler in
        # `_run_batch`), poisoning the whole batch with CancelledError. A
        # max_batch flush that drains first leaves the pending timer to fire on
        # an empty `pending` (→ early return), so cancelling it is just cleanup,
        # not correctness — and must be skipped for self.
        if timer is not None and not timer.done() and timer is not asyncio.current_task():
            timer.cancel()
        if not batch:
            return
        await self._run_batch(batch)

    async def _run_batch(self, batch):
        # T1.4: drop waiters whose item aged past request_timeout_s BEFORE
        # the handler runs — fan a BatchItemTimeout (504) to each, never a
        # silent skip (an unresolved future hangs the client: the M4 bug).
        # Disabled when request_timeout_s == 0 (the default — no behavior
        # change). Saves the upstream call from carrying dead items, and
        # the slot is freed for live traffic.
        if self.request_timeout_s > 0.0 and batch:
            now = time.monotonic()
            fresh = []
            for entry in batch:
                if now - entry[2] > self.request_timeout_s:
                    for f in entry[1]:
                        if not f.done():
                            f.set_exception(BatchItemTimeout(
                                f"@app.batch item exceeded request_timeout_ms "
                                f"({int(self.request_timeout_s * 1000)}ms) "
                                f"waiting for a batch slot"))
                else:
                    fresh.append(entry)
            batch = fresh
            if not batch:
                return
        # Each entry is (payload, [waiter_future, ...], enq): ONE upstream call
        # per UNIQUE payload (T1.1 dedup), result fanned to all its waiters.
        payloads = [e[0] for e in batch]
        # T1.3 pipeline gate: count this batch as in-flight the moment it
        # commits to running (BEFORE the semaphore await, so a batch BLOCKED
        # waiting for a slot still registers — the T1.5 submit-time shed reads
        # this counter and must see queued-but-not-yet-running batches too).
        # `inflight`/`sema` are None on the default unbounded path → ZERO extra
        # work (no counter math, no acquire), byte-identical to before T1.3.
        # The `finally` ALWAYS decrements so a frozen counter can't wedge the
        # pipeline closed (the M4 hangs-forever class, applied to the slot).
        if self.inflight is not None:
            self.inflight[0] += 1
        try:
            await self._call_and_fanout(batch, payloads)
        finally:
            if self.inflight is not None:
                self.inflight[0] -= 1

    async def _call_and_fanout(self, batch, payloads):
        """Run the handler ONCE for `batch` (under the T1.3 semaphore when
        set) and fan its result/exception to every waiter. Split out of
        `_run_batch` so the in-flight accounting wraps it in a single
        `try/finally` regardless of which fanout branch returns."""
        try:
            if self.sema is not None:
                # Hold the slot ACROSS the handler await so window k+1 fills
                # while batch k is upstream (Ray Serve batching.py:439). The
                # M4 lesson lives here: a bug holding this across the await
                # deadlocks/poisons waiters → the awaiting-handler load test.
                async with self.sema:
                    results = await self.handler(payloads)
            else:
                results = await self.handler(payloads)
        except BaseException as exc:  # noqa: BLE001 — fanout, see below
            # Whole-batch failure → every waiter sees the same exception.
            # We deliberately catch BaseException too: CancelledError
            # propagating to a SINGLE waiter would leave the rest
            # hanging forever, which is the real data-integrity bug.
            for entry in batch:
                for f in entry[1]:
                    if not f.done():
                        f.set_exception(exc)
            return
        if not isinstance(results, list) or len(results) != len(batch):
            err = RuntimeError(
                f"@app.batch handler returned "
                f"{type(results).__name__} of len "
                f"{len(results) if hasattr(results, '__len__') else '?'}; "
                f"expected list of len {len(batch)}"
            )
            for entry in batch:
                for f in entry[1]:
                    if not f.done():
                        f.set_exception(err)
            return
        for i, entry in enumerate(batch):
            for f in entry[1]:
                if not f.done():
                    f.set_result(results[i])


class _BatchRegistry:
    """Per-route container of `_BatchGroup`s, keyed by the user's
    `key(item)` value. Sits on the App so registry state survives
    across requests.

    SECURITY: `max_keys` caps the registry size so a malicious client
    can't inflate memory by sending an unbounded number of distinct
    keys. When exceeded, new requests get a clean BatchOverloadError (a
    sheddable load signal → 503 + Retry-After at the ASGI seam) — better
    than a slow OOM, and the client can back off rather than treating it
    as a 500 server bug (M15 T9).

    `size_fn` / `max_size` (optional, M15 T9): forwarded to each group so
    the flush trigger can be a byte/token BUDGET, not just a count."""

    def __init__(self, handler, window_s: float, max_batch: int,
                 key_fn: Callable | None, max_keys: int = 1024,
                 size_fn: Callable[[list], int] | None = None,
                 max_size: int = 0, request_timeout_s: float = 0.0,
                 dedup_key: Callable | None = None,
                 max_concurrent_batches: int = 0):
        self.handler = handler
        self.window_s = window_s
        self.max_batch = max_batch
        self.key_fn = key_fn
        self.max_keys = max_keys
        self.size_fn = size_fn
        self.max_size = max_size
        self.request_timeout_s = request_timeout_s
        self.dedup_key = dedup_key
        # T1.3/T1.5: pipeline concurrency cap, SHARED across every group of
        # this registry. 0 (or negative) → UNBOUNDED = today's behavior: no
        # semaphore, no in-flight counter, no compute shed (sema/inflight stay
        # None and the hot path is byte-identical to pre-T1.3).
        self.max_concurrent_batches = (
            max_concurrent_batches if max_concurrent_batches and
            max_concurrent_batches > 0 else 0)
        if self.max_concurrent_batches:
            # asyncio.Semaphore/Lock construct without a running loop on 3.10+
            # (same rationale as _BatchGroup.__post_init__ eager lock).
            self._sema: asyncio.Semaphore | None = asyncio.Semaphore(
                self.max_concurrent_batches)
            self._inflight: list | None = [0]  # registry-wide running-batch count
        else:
            self._sema = None
            self._inflight = None
        self._groups: dict = {}
        # Eager construction (see _BatchGroup.__post_init__ rationale).
        self._mu: asyncio.Lock = asyncio.Lock()

    async def submit(self, payload):
        k = self.key_fn(payload) if self.key_fn else "_default_"
        async with self._mu:
            # T1.5 compute-overload shed: when a concurrency cap is set AND
            # every slot is taken (N batches already running-or-queued upstream),
            # shed THIS NEW arrival with a sheddable 503 (BatchOverloadError →
            # Retry-After + X-Lancelot-Shed-Reason). We shed the new arrival ONLY
            # — items already attached to a pending/in-flight batch are never
            # touched (they keep their slot and complete). Checked under `_mu`
            # against the registry-wide counter, so the read is race-free vs the
            # group flushes that increment it. Default (cap=0) → never sheds.
            if self.max_concurrent_batches and \
                    self._inflight[0] >= self.max_concurrent_batches:
                raise BatchOverloadError(
                    f"@app.batch pipeline saturated "
                    f"({self.max_concurrent_batches} concurrent batches "
                    "in flight); retry shortly or raise "
                    "max_concurrent_batches.",
                    retry_after=1,
                    shed_reason="batch-overload",
                )
            grp = self._groups.get(k)
            if grp is None:
                if len(self._groups) >= self.max_keys:
                    raise BatchOverloadError(
                        f"@app.batch registry cap reached "
                        f"({self.max_keys} distinct keys); "
                        "increase max_keys or coarsen the key function.",
                        retry_after=1,
                        shed_reason="batch-keys",
                    )
                # SNAPSHOT window_s / max_batch / size budget into the group
                # at construction — a later live retune of THIS registry
                # can't retroactively change a group's trigger (T9). The
                # pipeline semaphore + in-flight counter are SHARED (not
                # snapshotted) so the cap is registry-wide across all keys.
                grp = _BatchGroup(self.handler, self.window_s, self.max_batch,
                                  size_fn=self.size_fn, max_size=self.max_size,
                                  request_timeout_s=self.request_timeout_s,
                                  dedup_key=self.dedup_key,
                                  sema=self._sema, inflight=self._inflight)
                self._groups[k] = grp
        return await grp.submit(payload)
