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
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class _BatchGroup:
    """One bucket. There's a separate group per `key`-value the user
    function partitions on. Mutex-protected so submissions and the
    flush-timer don't race on `pending`."""
    handler: Callable[..., Any]
    window_s: float
    max_batch: int
    pending: list = field(default_factory=list)  # list[(payload, Future)]
    timer_task: asyncio.Task | None = None
    lock: asyncio.Lock | None = None

    def __post_init__(self):
        # Eager lock so two concurrent first-submissions can't each
        # create their own Lock and break mutual exclusion. Python 3.10+
        # allows asyncio.Lock() construction without a running loop.
        if self.lock is None:
            self.lock = asyncio.Lock()

    async def submit(self, payload):
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        flush_now = False
        async with self.lock:
            self.pending.append((payload, fut))
            if len(self.pending) >= self.max_batch:
                flush_now = True
            elif self.timer_task is None:
                # First in the window: arm the timer.
                self.timer_task = asyncio.ensure_future(self._wait_then_flush())
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
            if self.timer_task is not None and not self.timer_task.done():
                self.timer_task.cancel()
            self.timer_task = None
        if not batch:
            return
        await self._run_batch(batch)

    async def _run_batch(self, batch):
        payloads = [p for p, _ in batch]
        try:
            results = await self.handler(payloads)
        except BaseException as exc:  # noqa: BLE001 — fanout, see below
            # Whole-batch failure → every waiter sees the same exception.
            # We deliberately catch BaseException too: CancelledError
            # propagating to a SINGLE waiter would leave the rest
            # hanging forever, which is the real data-integrity bug.
            for _, f in batch:
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
            for _, f in batch:
                if not f.done():
                    f.set_exception(err)
            return
        for i, (_, f) in enumerate(batch):
            if not f.done():
                f.set_result(results[i])


class _BatchRegistry:
    """Per-route container of `_BatchGroup`s, keyed by the user's
    `key(item)` value. Sits on the App so registry state survives
    across requests.

    SECURITY: `max_keys` caps the registry size so a malicious client
    can't inflate memory by sending an unbounded number of distinct
    keys. When exceeded, new requests get a clean RuntimeError (which
    surfaces as 500 to the client) — better than a slow OOM."""

    def __init__(self, handler, window_s: float, max_batch: int,
                 key_fn: Callable | None, max_keys: int = 1024):
        self.handler = handler
        self.window_s = window_s
        self.max_batch = max_batch
        self.key_fn = key_fn
        self.max_keys = max_keys
        self._groups: dict = {}
        # Eager construction (see _BatchGroup.__post_init__ rationale).
        self._mu: asyncio.Lock = asyncio.Lock()

    async def submit(self, payload):
        k = self.key_fn(payload) if self.key_fn else "_default_"
        async with self._mu:
            grp = self._groups.get(k)
            if grp is None:
                if len(self._groups) >= self.max_keys:
                    raise RuntimeError(
                        f"@app.batch registry cap reached "
                        f"({self.max_keys} distinct keys); "
                        "increase max_keys or coarsen the key function."
                    )
                grp = _BatchGroup(self.handler, self.window_s, self.max_batch)
                self._groups[k] = grp
        return await grp.submit(payload)
