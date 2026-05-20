"""KV-cache-friendly JSON canonicalization (Tier-C.C1).

`dumps_canonical(obj)` emits sorted-key, compact (no spaces), non-NaN JSON
bytes — the deterministic representation. Useful when the SAME prompt
payload must hash identically across runs so an upstream LLM's prompt
cache hits (Anthropic prompt caching, OpenAI completions cache, vLLM
prefix cache, Manus KV-cache).

We do NOT do any caching ourselves — that's an upstream/user responsibility.
We just give the user a deterministic byte-encoder so they CAN cache without
fighting Python's dict-order surprises.

Cardinal rule: pydantic only. This is stdlib `json` with the right flags.
"""
import json
from typing import Any


def dumps_canonical(obj: Any) -> bytes:
    """Stable JSON bytes:
      - keys sorted lexicographically (Python's sort, byte-equivalent
        across runs/builds — `json.dumps(sort_keys=True)`)
      - compact separators (`","` and `":"`, no whitespace)
      - `allow_nan=False` — NaN/Infinity are NOT valid JSON; emit raises
        so the user doesn't silently produce a non-RFC-8259 payload
      - `ensure_ascii=False` — emit UTF-8 directly (shorter; identical
        semantics)
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def loads(raw: bytes | str) -> Any:
    """The inverse — accepts bytes/str, no surprises. Same as json.loads."""
    return json.loads(raw)
