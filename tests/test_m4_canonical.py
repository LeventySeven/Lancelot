"""M4 Tier-V2.3 — C1 KV-cache helper (`dumps_canonical`) validation.

The C1 claim is DETERMINISM: the same logical payload must serialize to identical
bytes regardless of dict insertion order, so an upstream prompt/KV cache (Anthropic
/ OpenAI / vLLM prefix) hits instead of missing on Python's dict-order surprises.
Correctness tests pin examples; this is a property test (order-invariance over
many randomized structures) plus an honest cost measurement.
"""
import json
import math
import random
import time

import pytest

from lancelot.canonical import dumps_canonical, loads


def _rand_json(depth, rng):
    """A random JSON-able value; dicts get string keys, lists stay ordered."""
    if depth <= 0:
        return rng.choice([rng.randint(-1000, 1000), rng.random(),
                           rng.choice(["", "x", "ünïcode", "a b"]), True, False, None])
    kind = rng.choice(["dict", "list", "scalar"])
    if kind == "scalar":
        return _rand_json(0, rng)
    if kind == "list":
        return [_rand_json(depth - 1, rng) for _ in range(rng.randint(0, 4))]
    return {f"key_{rng.randint(0, 9)}_{i}": _rand_json(depth - 1, rng)
            for i in range(rng.randint(0, 5))}


def _shuffle_dict_keys(obj, rng):
    """Rebuild with dict keys in RANDOM order at every level (lists keep order
    — JSON lists are ordered; only dict key order is non-deterministic)."""
    if isinstance(obj, dict):
        items = list(obj.items())
        rng.shuffle(items)
        return {k: _shuffle_dict_keys(v, rng) for k, v in items}
    if isinstance(obj, list):
        return [_shuffle_dict_keys(x, rng) for x in obj]
    return obj


def test_v2_3_canonical_is_order_invariant_property():
    rng = random.Random(1234)
    for _ in range(200):
        obj = _rand_json(4, rng)
        base = dumps_canonical(obj)
        # Re-encoding the SAME object is stable, and ANY dict-key reordering at
        # any nesting level yields byte-IDENTICAL output — the cache-hit property.
        assert dumps_canonical(obj) == base
        for _ in range(4):
            assert dumps_canonical(_shuffle_dict_keys(obj, rng)) == base


def test_v2_3_canonical_roundtrips_and_is_compact():
    obj = {"b": 1, "a": [3, 2, {"z": 1, "y": 2}], "ünïcode": " key"}
    raw = dumps_canonical(obj)
    assert loads(raw) == obj                      # inverse
    assert b" " not in raw.replace(b" key", b"")  # compact (no separator spaces)
    assert raw.index(b'"a"') < raw.index(b'"b"')  # keys sorted


def test_v2_3_canonical_rejects_nan_inf():
    # NaN/Infinity are not valid JSON — must raise, not silently emit garbage
    # that would poison a cache key.
    for bad in (float("nan"), float("inf"), -math.inf):
        with pytest.raises(ValueError):
            dumps_canonical({"x": bad})


def test_v2_3_canonical_cost_is_a_small_constant_factor():
    # Honest cost: determinism (sort_keys) adds overhead vs plain dumps. Measure
    # it on a representative payload; assert it's a small constant factor, not a
    # cliff. CPython's json sort_keys is C-level, so this is cheap.
    payload = {f"field_{i}": {"v": i, "tags": list(range(10)), "name": f"item-{i}"}
               for i in range(200)}
    n = 500

    def timeit(fn):
        t0 = time.perf_counter()
        for _ in range(n):
            fn(payload)
        return time.perf_counter() - t0

    plain = timeit(lambda p: json.dumps(p).encode())
    canon = timeit(dumps_canonical)
    # Generous bound — the point is "cheap determinism", not a tight number.
    assert canon < plain * 3.0, f"canonical {canon:.3f}s vs plain {plain:.3f}s (>3x)"
