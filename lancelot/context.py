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


# ----- M7 T2.1 + T3.3 context additions ----------------------------------

# M7 T2.1 (YC-RA Sim.ai pattern): per-request cost accumulator. Handlers
# (or middleware) call `cost.set(cost.get() + n)` after each upstream
# call to accumulate $/request; the ASGI seam emits the final value as
# X-Lancelot-Cost-USD response header + `lancelot.http.cost.usd` metric.
# Lancelot doesn't ship cost RATES (those depend on which provider/model
# the user picks) — we ship the accumulator + emission; users compute.
cost_usd: contextvars.ContextVar[float] = contextvars.ContextVar(
    "lancelot.cost_usd", default=0.0
)

# M7 T2.1 companion: token counters (in/out). Same pattern; complement
# to cost_usd for handlers that bill per-token AND per-request.
tokens_in: contextvars.ContextVar[int] = contextvars.ContextVar(
    "lancelot.tokens_in", default=0
)
tokens_out: contextvars.ContextVar[int] = contextvars.ContextVar(
    "lancelot.tokens_out", default=0
)

# M7 T3.3 (Infisical 3rd-actor pattern): per-request agent identity.
# Populated from a configurable header (default `X-Agent-Id`) when the
# request comes from an AI agent rather than a human or a machine. Used
# by logs/metrics for fan-out attribution + by per-tenant rate-limit
# middleware to bucket cost/quota per agent. Same "-" sentinel as
# request_id so reads outside a request are safe.
agent_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "lancelot.agent_id", default="-"
)

# M7 T2.4 (Hightouch pattern): tenant identity bounded by
# LANCELOT_TENANT_LABEL_MAX. Set by middleware; read by RED metrics +
# any per-tenant cap / cost-budget circuit breaker. Cardinality control
# (over-cap bucketing to `_overflow_`) is wired in the metrics emit path.
tenant_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "lancelot.tenant_id", default="-"
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
