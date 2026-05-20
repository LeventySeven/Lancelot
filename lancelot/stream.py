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
            out.append(f"id: {self.id}")
        out.append(f"event: {self.name}")
        out.append("data: " + _json.dumps(self.data, separators=(",", ":")))
        out.append("")
        out.append("")
        return "\n".join(out).encode("utf-8")
