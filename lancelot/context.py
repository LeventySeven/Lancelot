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
import re
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


# W3C Trace Context (Tier-B cloud observability). When a request arrives with a
# `traceparent` header, the ASGI seam exposes its trace-id here (for the user's
# logs/metrics to correlate this service into the distributed trace) and stashes
# the raw header in `trace_parent` so `lancelot.llm.TransportPool` forwards it on
# the outbound LLM call — the AI request joins the SAME trace. This is
# lightweight CONTEXT PROPAGATION with no dep; full span export is the user's
# OpenTelemetry job (we don't reimplement it — cardinal-rule corollary).
trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "lancelot.trace_id", default="-"
)
trace_parent: contextvars.ContextVar = contextvars.ContextVar(
    "lancelot.trace_parent", default=None
)

_TRACEPARENT_RE = re.compile(
    r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$"
)


def parse_traceparent(value):
    """Validate a W3C `traceparent` header value → its 32-hex trace-id, or
    `None` if absent/malformed/all-zero. Strict per the spec shape
    `version-traceid-spanid-flags`; case-insensitive."""
    if not value:
        return None
    m = _TRACEPARENT_RE.match(value.strip().lower())
    if not m:
        return None
    trace, span = m.group(1), m.group(2)
    if trace == "0" * 32 or span == "0" * 16:
        return None  # all-zero ids are invalid per the spec
    return trace
