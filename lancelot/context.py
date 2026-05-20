"""Per-request ContextVars (Tier-B.B3 / research-distillation).

A small surface user middleware + handlers + logging can pull from to thread
request-scoped data through any await without passing it explicitly. Set by
the ASGI seam at request entry, propagated automatically by asyncio's
copy-context semantics on every `await`.

Why ContextVars and not threadlocals: asyncio Tasks each get their own
context, so a long-running concurrent task tree (e.g. `lancelot.gather`)
does NOT bleed across requests. This is Robyn's pattern (asyncio.rs:14-49),
matched correctly here for Python 3.11+.
"""
import contextvars
import uuid

# Set in asgi._http on entry. The default-arg form returns "-" when read
# outside a request (logs in lifespan / background tasks see "-" rather
# than blowing up with LookupError).
request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "lancelot.request_id", default="-"
)
# Set whenever the user defines per-route auth that resolves a principal —
# kept as a generic stash; framework does not interpret the value.
principal: contextvars.ContextVar[object] = contextvars.ContextVar(
    "lancelot.principal", default=None
)


def new_request_id() -> str:
    """Default request-id generator (UUIDv4 hex). The ASGI seam prefers
    the incoming `X-Request-ID` / `X-Correlation-ID` header if present, and
    falls back to this. uuid4() is sufficient for trace correlation — we
    do NOT use it for security."""
    return uuid.uuid4().hex
