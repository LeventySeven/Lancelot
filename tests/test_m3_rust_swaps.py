"""M3 Rust hot-path swap tests (R2 lazy request proxy + R3 streaming pump).

These exercise the _native FFI seam in-process (no HTTP server) — the same
pattern as the M0 _drive concurrency test. They verify the GIL choreography
+ correctness of the Rust-side request proxy and async-gen pump.
"""
import lancelot._native as _native


# ---------- R2: lazy request proxy ------------------------------------

def test_r2_request_exposes_real_method_path_query_headers():
    """The Request pyclass surfaces the REAL method/path/query/headers/body
    (M0 hardcoded "POST"/"/echo"). Build one via the test constructor and
    verify the lazy getters. (The full hyper→Request threading is covered by
    the server path; this pins the pyclass contract.)"""
    req = _native._test_make_request(b"GET", b"/users/42", b'{"x": 1}')
    assert req.method == "GET"
    assert req.path == "/users/42"
    assert req.body == b'{"x": 1}'
    # json() decodes lazily + caches.
    assert req.json() == {"x": 1}
    assert req.json() == {"x": 1}  # cached path
    # headers/query default to empty for the test constructor.
    assert req.headers == {}
    assert req.query_params == {}
    assert req.query_string == ""


# ---------- R3: streaming pump ----------------------------------------

def _make_fninfo(fn):
    # FnInfo(handler, is_async, arity) — the registration contract.
    return _native.FnInfo(fn, True, 0)


def test_r3_pump_collects_async_gen_chunks_in_order():
    """A Python async generator is pulled chunk-by-chunk by the Rust pump
    (GIL dropped across each __anext__ await); chunks arrive in order, and
    StopAsyncIteration ends the stream cleanly."""
    async def gen():
        for i in range(5):
            yield f"chunk-{i}".encode()

    chunks = _native._drive_stream(_make_fninfo(gen))
    assert chunks == [b"chunk-0", b"chunk-1", b"chunk-2", b"chunk-3", b"chunk-4"]


def test_r3_pump_handles_str_chunks():
    """str chunks are utf-8 encoded by the Rust chunk extractor."""
    async def gen():
        yield "hello "
        yield "world"

    chunks = _native._drive_stream(_make_fninfo(gen))
    assert chunks == [b"hello ", b"world"]


def test_r3_pump_empty_stream():
    """An async-gen that yields nothing produces an empty chunk list (clean
    StopAsyncIteration on the first pull)."""
    async def gen():
        return
        yield  # unreachable — makes this an async-gen

    assert _native._drive_stream(_make_fninfo(gen)) == []


def test_r3_pump_json_lines_for_objects():
    """Non-bytes/str chunks are JSON-encoded + newline (JSON-lines), matching
    the ASGI seam's application/jsonl auto-wrap."""
    async def gen():
        yield {"a": 1}
        yield {"b": 2}

    chunks = _native._drive_stream(_make_fninfo(gen))
    # pydantic_core.to_json is compact; trailing newline added by the pump.
    assert chunks == [b'{"a":1}\n', b'{"b":2}\n']


def test_r3_pump_awaits_between_chunks():
    """The pump drives a gen that awaits between yields (real async work) —
    proving each __anext__ coroutine is bridged + awaited with the GIL
    dropped, not just a sync generator."""
    import asyncio

    async def gen():
        for i in range(3):
            await asyncio.sleep(0)  # yield to the loop
            yield str(i).encode()

    assert _native._drive_stream(_make_fninfo(gen)) == [b"0", b"1", b"2"]
