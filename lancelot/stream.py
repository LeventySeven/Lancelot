"""M2-P4 — typed event-stream sugar. A handler yielding `Event(name,
payload)` becomes an SSE response with `event:`/`data:` framing emitted
by the framework. Replaces the 9+ product hand-rolls of
`yield f"event: ...\\ndata: {json}\\n\\n"`.

No `resumable=` (strict synthesis — zero product citations for
Last-Event-Id replay). Cancellation: when the client disconnects, the
async generator gets `asyncio.CancelledError` naturally; in addition,
`request.cancelled` is set so the handler can short-circuit upstream
LLM calls."""
import json as _json
from dataclasses import dataclass


def _clean_field(s: str) -> str:
    """Strip CR/LF/NUL + control chars from an SSE field value. A newline in
    `id:`/`event:` smuggles an extra frame; a NUL in `id:` silently resets the
    client's lastEventId (WHATWG SSE §9.2.6). Same A3 strip used for
    media_type / cookies."""
    return "".join(ch for ch in str(s) if ch >= " " and ch != "\x7f")


@dataclass
class Event:
    """One server-sent event. `name` becomes `event:`; `data` is
    JSON-serialized into `data:`. Sentinel sub-types are encouraged
    (`Event('delta', {...})`, `Event('done', {})`)."""
    name: str
    data: object = None
    id: str | None = None

    def render(self) -> bytes:
        out = []
        if self.id is not None:
            out.append(f"id: {_clean_field(self.id)}")
        out.append(f"event: {_clean_field(self.name)}")
        out.append("data: " + _json.dumps(self.data, separators=(",", ":")))
        out.append("")
        out.append("")
        return "\n".join(out).encode("utf-8")


# M15 T7 (#5) — SSE-of-models framing + a terminal `[DONE]` sentinel.
#
# `@app.stream(sse=True, done_sentinel="[DONE]")` wraps the user's async
# generator in `sse_adapter` (below). The wrapper does the framing in PYTHON
# and yields already-framed `bytes`, so BOTH transports stay byte-identical by
# construction:
#   * the uvicorn ASGI seam (`asgi._stream_chunk_bytes`) passes `bytes` through;
#   * the native R3 pump (`src/stream.rs` `chunk_to_bytes`) passes `bytes`
#     through too.
# The R3 pump (on-loop `_drain` + same-thread `try_push` + bounded backpressure)
# is therefore UNTOUCHED — no per-chunk cross-thread bridging is reintroduced
# (CLAUDE.md Tier-D: do not regress R3). This is the plan's sanctioned approach:
# emit the sentinel from the Python seam so both pumps produce a terminal
# `data: <done_sentinel>\n\n`, with a single source of truth for the framing.

_DEFAULT_DONE_SENTINEL = "[DONE]"


def _sse_data_frame(payload_json: bytes) -> bytes:
    """Wrap pre-serialized JSON bytes in one `data: <json>\n\n` SSE frame
    (the shape an EventSource/browser client parses; a bare JSON line is not)."""
    return b"data: " + payload_json + b"\n\n"


def _model_to_json(obj) -> bytes:
    """Serialize a bare model/dict to compact JSON bytes via the SAME Rust
    pydantic-core seam the 200 path uses (`validation._wire_json` → orjson when
    present, else `_native._to_json`, else stdlib) — NOT `model_dump_json`. This
    keeps the streamed model bytes byte-identical to the unary-response bytes."""
    from lancelot.validation import _wire_json
    return _wire_json(obj)


def sse_adapter(gen, done_sentinel=_DEFAULT_DONE_SENTINEL):
    """Wrap a user async generator so that, under `@app.stream(sse=True)`:

      * an `Event` / `bytes` / `str` yield is passed through UNCHANGED
        (byte-identical backward compatibility — only the bare-model path
        changes);
      * any other yielded object (a Pydantic model, a dict, ...) is framed as
        one `data: <pydantic-core json>\n\n` SSE frame;
      * on CLEAN exhaustion a terminal `data: <done_sentinel>\n\n` frame is
        emitted (default `[DONE]`; `done_sentinel=None` disables it).

    The terminal sentinel is emitted ONLY on clean exhaustion — if the user
    generator raises, the exception propagates (the seam's error-frame path
    handles it) and NO `[DONE]` is sent (a half-stream must not look complete).

    Cancellation: a client disconnect cancels THIS wrapper exactly as it would
    the user generator (the wrapper's `async for` is suspended at the same
    `__anext__`), so the R3 ≤1-extra-chunk contract is preserved.
    """
    async def _wrapped():
        async for item in gen:
            if isinstance(item, (bytes, bytearray)):
                yield bytes(item)
            elif isinstance(item, str):
                yield item.encode("utf-8")
            elif isinstance(item, Event) or callable(getattr(item, "render", None)):
                # Event (or any render()-able): keep its own framing verbatim.
                yield item
            else:
                # Bare model / dict / scalar → one SSE data frame.
                yield _sse_data_frame(_model_to_json(item))
        # Clean exhaustion (no exception): emit the terminal sentinel.
        if done_sentinel is not None:
            yield _sse_data_frame(_clean_field(done_sentinel).encode("utf-8"))

    wrapped = _wrapped()
    # The native R3 path reads `media_type` off the result object to pick the
    # response content-type (src/stream.rs::stream_media_type). An async
    # generator object has no writable attributes, so expose the SSE media type
    # via a tiny proxy that forwards the async-iterator protocol and carries
    # `media_type`. The uvicorn path keys off `route.is_stream` and always sends
    # `text/event-stream`, so this only affects the native path's header.
    return _SSEStream(wrapped)


class _SSEStream:
    """Async-iterable proxy that carries `media_type='text/event-stream'` for
    the native R3 path's content-type detection. Forwards `__aiter__`/`__anext__`
    to the wrapped generator (no per-chunk overhead beyond one attribute hop)
    and forwards `aclose()` so cancellation/teardown reaches the user generator."""
    media_type = "text/event-stream"

    def __init__(self, agen):
        self._agen = agen

    def __aiter__(self):
        return self._agen.__aiter__()

    def __anext__(self):
        return self._agen.__anext__()

    def aclose(self):
        return self._agen.aclose()
