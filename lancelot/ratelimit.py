"""M17 P3 — inbound rate-limiter middleware (`RateLimit`).

An OPT-IN, pure-Python ASGI middleware that token-buckets *inbound* requests
per key and sheds the excess with **429 + Retry-After + RateLimit-Limit /
RateLimit-Remaining / RateLimit-Reset**. This is the missing INBOUND limiter:
every other 429 in the tree is OUTBOUND (`llm.py` upstream throttling), the
`@app.batch` admission shed, or the connection cap; `tenant.py` is per-tenant
*concurrency* (a Semaphore), not rate-over-window.

Wiring (typical)::

    app = lancelot.App()
    app.add_middleware(RateLimit, rate="100/minute", key="ip", burst=20)

Default behavior is byte-identical when the middleware is NOT added (it is a
pure escape-hatch wrapper, like CORS / TenantConcurrencyLimiter).

================================================================================
IMPORTANT — PER-PROCESS BY DEFAULT (read this before you rely on it)
================================================================================
Lancelot's mandated deploy model is MULTIPROCESS (N worker processes behind
`SO_REUSEPORT` / a proxy — the Rust server anti-scales on threads because of the
GIL). The default in-process backend keeps its token buckets in **this
worker's memory only**. With W workers a client can therefore burst up to
`W × burst` before any single worker sheds it — the effective global limit is
W times looser than the number you configured. For a per-worker safety valve
(stop one process from being buried) that is fine and is the common case. For a
*global* limit shared across the fleet, supply a shared `backend=` (e.g. a
Redis-backed `TokenBucketBackend`). **Lancelot does NOT ship a Redis dependency
and does NOT grow a rule-DSL** — `backend=` is a tiny 1-method hook you wire to
your own store. See `TokenBucketBackend` below.
================================================================================

The bucket math mirrors `lancelot.llm._TokenBucket`, but that one *blocks*
(awaits until a token frees — correct for an OUTBOUND caller pacing itself).
An inbound limiter must REJECT immediately, so this module carries a small
stdlib-only `take()` that returns a decision instead of sleeping. Keeping it
here also keeps `import lancelot.ratelimit` pydantic-only (no httpx).
"""
import time
from typing import Callable, Dict, Optional, Tuple, Union

__all__ = ["RateLimit", "TokenBucketBackend", "InProcessTokenBucketBackend"]


_UNITS = {
    "second": 1.0, "sec": 1.0, "s": 1.0,
    "minute": 60.0, "min": 60.0, "m": 60.0,
    "hour": 3600.0, "hr": 3600.0, "h": 3600.0,
    "day": 86400.0, "d": 86400.0,
}


def _parse_rate(rate: Union[str, Tuple[int, float]]) -> Tuple[int, float]:
    """'100/minute' -> (100, 60.0)  (count, window_seconds).

    Also accepts a pre-parsed ``(count, window_seconds)`` tuple. The refill
    rate is ``count / window`` tokens/second.
    """
    if isinstance(rate, tuple):
        count, window = rate
        count = int(count)
        window = float(window)
        if count <= 0 or window <= 0:
            raise ValueError("rate count and window must both be > 0")
        return count, window
    if not isinstance(rate, str) or "/" not in rate:
        raise ValueError(
            "rate must look like '100/minute' or a (count, window_s) tuple")
    num_s, _, unit_s = rate.partition("/")
    num_s = num_s.strip()
    unit_s = unit_s.strip().lower()
    try:
        count = int(num_s)
    except ValueError:
        raise ValueError(f"rate count {num_s!r} is not an integer") from None
    # Allow '5/30s' style explicit-second windows too.
    if unit_s.endswith("s") and unit_s[:-1].isdigit():
        window = float(unit_s[:-1])
    elif unit_s in _UNITS:
        window = _UNITS[unit_s]
    else:
        raise ValueError(f"unknown rate unit {unit_s!r}")
    if count <= 0 or window <= 0:
        raise ValueError("rate count and window must both be > 0")
    return count, window


class TokenBucketBackend:
    """The pluggable backend interface (the `backend=` hook).

    Implement a single method to share buckets across the fleet (e.g. Redis).
    Lancelot ships NO such backend (no Redis dependency) — this is the contract
    your own implementation honors. It MUST be atomic per key for correctness
    under concurrency (a Redis Lua script or `INCR`+`EXPIRE` is the usual
    shape).
    """

    def take(self, key: str, capacity: float,
             refill_per_sec: float) -> Tuple[bool, float, float]:
        """Attempt to spend ONE token for `key`.

        Returns ``(allowed, remaining, reset_after_seconds)``:
          - allowed: True if a token was available and spent.
          - remaining: tokens left after this call (>= 0).
          - reset_after_seconds: seconds until the NEXT token is available
            (0.0 when allowed and tokens remain; otherwise the wait the client
            should respect — drives Retry-After / RateLimit-Reset).
        """
        raise NotImplementedError


class InProcessTokenBucketBackend(TokenBucketBackend):
    """Default backend: per-PROCESS token buckets in a plain dict.

    Single asyncio loop per process → cooperative, single-threaded; bucket
    state mutates only synchronously between awaits, so no lock is needed
    (same reasoning as `llm._TokenBucket`). The dict grows by distinct key;
    bound cardinality by choosing `key` wisely (client IP / a hashed api-key,
    NOT arbitrary user input).

    Under-counts on the multiprocess deploy — see the module docstring.
    """

    __slots__ = ("_buckets",)

    def __init__(self) -> None:
        # key -> [tokens, last_refill_monotonic]
        self._buckets: Dict[str, list] = {}

    def take(self, key: str, capacity: float,
             refill_per_sec: float) -> Tuple[bool, float, float]:
        now = time.monotonic()
        b = self._buckets.get(key)
        if b is None:
            b = [capacity, now]            # start full (one burst allowed)
            self._buckets[key] = b
        tokens, last = b
        tokens = min(capacity, tokens + (now - last) * refill_per_sec)
        b[1] = now
        if tokens >= 1.0:
            tokens -= 1.0
            b[0] = tokens
            # If the bucket is now empty, the next token arrives in
            # 1/refill seconds; otherwise one is already available.
            reset_after = 0.0 if tokens >= 1.0 else (1.0 / refill_per_sec)
            return True, tokens, reset_after
        b[0] = tokens
        # Seconds until enough fractional tokens accrue to reach 1.0.
        reset_after = (1.0 - tokens) / refill_per_sec
        return False, 0.0, reset_after


def _client_ip(scope) -> str:
    client = scope.get("client")
    if client and len(client) >= 1 and client[0]:
        return str(client[0])
    return "unknown"


def _api_key(scope) -> str:
    """Extract an API-key-ish identity from headers (single pass).

    Order: ``X-API-Key`` header, else a Bearer/other token in
    ``Authorization``. Falls back to the client IP so an unauthenticated
    flood is still bucketed (per-IP) rather than all sharing one '' key.
    """
    authz = None
    for k, v in scope.get("headers", ()):
        if k == b"x-api-key":
            try:
                val = v.decode("latin1").strip()
            except Exception:  # noqa: BLE001
                val = ""
            if val:
                return val
        elif k == b"authorization":
            authz = v
    if authz is not None:
        try:
            val = authz.decode("latin1").strip()
        except Exception:  # noqa: BLE001
            val = ""
        if val:
            # 'Bearer xyz' -> 'xyz'; bare token -> itself.
            parts = val.split(None, 1)
            return parts[1] if len(parts) == 2 else val
    return _client_ip(scope)        # anonymous → bucket by IP


class RateLimit:
    """Pure-ASGI inbound rate-limiter middleware (opt-in).

    Args:
        app: the inner ASGI app (set by ``add_middleware``).
        rate: ``'100/minute'`` (or ``(count, window_seconds)``). The sustained
            refill rate is ``count / window`` requests/second.
        key: how to bucket. ``'ip'`` (client address), ``'api_key'``
            (X-API-Key / Authorization token, falling back to IP), or a
            callable ``scope -> str`` for a custom dimension.
        burst: bucket capacity (max instantaneous burst). Defaults to the
            window's ``count`` when unset, so a client may burst one window's
            worth then is paced at the refill rate.
        backend: a ``TokenBucketBackend`` for a shared/global limit (e.g.
            Redis). Defaults to per-process (see the module docstring caveat).

    Reverse-proxy / load-balancer caveat (companion to the per-process /
    multiprocess caveat above): with ``key='ip'`` the bucket key is
    ``scope['client']``, which behind a proxy / LB is the **PROXY's** IP, not
    the real client's. So every client behind the same proxy shares ONE bucket
    and is throttled together — a safe **over**-throttle (the limit binds
    sooner than intended), never a bypass (no client gets MORE than the policy).
    To limit by the real client IP behind a **TRUSTED** proxy, pass a
    ``key=callable(scope)->str`` that reads a validated ``X-Forwarded-For`` (or
    your proxy's equivalent) from ``scope['headers']``. Only trust that header
    when the proxy is under your control and strips/overwrites any
    client-supplied value — an attacker can otherwise spoof ``X-Forwarded-For``
    to dodge or poison the limit.

    On shed: emits **429** with ``Retry-After`` (delta-seconds), and the
    ``RateLimit-Limit`` / ``RateLimit-Remaining`` / ``RateLimit-Reset`` headers.
    """

    def __init__(self, app, *,
                 rate: Union[str, Tuple[int, float]] = "100/minute",
                 key: Union[str, Callable] = "ip",
                 burst: Optional[int] = None,
                 backend: Optional[TokenBucketBackend] = None):
        self.app = app
        count, window = _parse_rate(rate)
        self._refill = count / window                  # tokens/second
        self._capacity = float(burst) if burst is not None else float(count)
        if self._capacity < 1.0:
            raise ValueError("burst must be >= 1")
        # Reported as RateLimit-Limit (the policy ceiling the client sees).
        self._limit_str = str(int(self._capacity))
        self._backend = backend or InProcessTokenBucketBackend()
        # Resolve the key extractor once.
        if callable(key):
            self._keyfn = key
            self._prefix = "fn"
        elif key == "ip":
            self._keyfn = _client_ip
            self._prefix = "ip"
        elif key == "api_key":
            self._keyfn = _api_key
            self._prefix = "ak"
        else:
            raise ValueError(
                "key must be 'ip', 'api_key', or a callable(scope)->str")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            raw = self._keyfn(scope)
        except Exception:  # noqa: BLE001 — a broken key fn must never 500
            raw = "unknown"
        bucket_key = f"{self._prefix}:{raw}"
        allowed, remaining, reset_after = self._backend.take(
            bucket_key, self._capacity, self._refill)
        if allowed:
            return await self.app(scope, receive, send)
        # Shed. Retry-After / RateLimit-Reset are delta-seconds, ceil'd so a
        # client that waits the advertised time definitely has a token.
        retry = int(reset_after) + (1 if reset_after % 1 else 0)
        if retry < 1:
            retry = 1
        retry_b = str(retry).encode()
        body = b'{"detail":"Rate limit exceeded"}'
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"retry-after", retry_b),
            (b"ratelimit-limit", self._limit_str.encode()),
            (b"ratelimit-remaining", b"0"),
            (b"ratelimit-reset", retry_b),
        ]
        await send({"type": "http.response.start", "status": 429,
                    "headers": headers})
        await send({"type": "http.response.body", "body": body})
