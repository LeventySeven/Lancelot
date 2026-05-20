"""JSON nesting-depth guard (Tier-A.A3 / research-distillation 2.x).

Stops nested-bomb DoS bodies (`[[[[[...]]]]]` 10⁵ deep) before they reach
pydantic-core / stdlib `json.loads`, both of which happily recurse until the
C stack blows up. A single linear byte-scan with one counter — no allocation,
no copies — runs O(len(body)) and short-circuits the moment the limit is
exceeded.

The default limit is 128 (Robyn's value, `request.rs:271`). Configurable per
app via `App(json_max_depth=N)` once we expose the knob.
"""

# Strings are ignored: `{` / `[` inside a JSON string are not nesting.
# We track a simple state: in_string, prev_was_backslash.
_OPEN = (0x7B, 0x5B)   # b'{' b'['
_CLOSE = (0x7D, 0x5D)  # b'}' b']'
_QUOTE = 0x22          # b'"'
_BACKSLASH = 0x5C      # b'\\'


def exceeds_depth(raw: bytes, limit: int = 128) -> bool:
    """Return True iff `raw` is JSON whose maximum nesting depth would
    exceed `limit`. Treats strings literally; whitespace is ignored.
    Tolerant of trailing garbage — a JSON bomb wins as soon as the
    counter exceeds `limit`, even before the closing bracket."""
    depth = 0
    in_string = False
    escaped = False
    for b in raw:
        if in_string:
            if escaped:
                escaped = False
            elif b == _BACKSLASH:
                escaped = True
            elif b == _QUOTE:
                in_string = False
            continue
        if b == _QUOTE:
            in_string = True
            continue
        if b in _OPEN:
            depth += 1
            if depth > limit:
                return True
        elif b in _CLOSE:
            if depth > 0:
                depth -= 1
    return False
