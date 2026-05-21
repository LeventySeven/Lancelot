//! M3-R3 — the streaming Rust pump (re-architected; see benchmarks/).
//!
//! A Python async generator (`async def gen(): yield ...`) is written to a hyper
//! chunked body WITHOUT holding the GIL across any `.await` and WITHOUT a
//! per-chunk thread spawn (Robyn's documented defect, deep/04:23).
//!
//! ## Why the original per-`__anext__` design was replaced
//! The first implementation pulled ONE chunk per `into_future_with_locals(
//! gen.__anext__())` — i.e. one `call_soon_threadsafe` cross-thread round-trip
//! to the asyncio loop PER chunk (the pump ran on a tokio worker thread; the
//! loop runs elsewhere). Measured cost ~0.8 ms/chunk → streaming was a 16-26x
//! throughput cliff vs Starlette (fatal for LLM token streaming).
//!
//! ## Current design (m07-concurrency — on-loop drain + bounded backpressure)
//!  - A small Python coroutine `_drain(gen, sink)` is bridged to a Send Rust
//!    future ONCE for the whole stream. It runs ON the asyncio loop, so its
//!    `async for chunk in gen` drives the generator natively (zero per-chunk
//!    bridging) and each `sink.try_push(chunk)` is a SAME-THREAD synchronous
//!    `try_send` into a BOUNDED channel (depth `stream_buffer()`, default 256).
//!  - Backpressure: a full channel → `try_push` returns False → the coro
//!    `await`s `sink.await_capacity()` (a GIL-free Rust `reserve().await`, one
//!    cross-thread hop but ONLY when the consumer is slow) → retry. No unbounded
//!    buffering; the bound propagates to the upstream LLM.
//!  - End: the `_drain` `finally: sink.close()` drops the sender → body EOF.
//!    Client disconnect → receiver drops → `try_send` returns Closed (swallowed)
//!    → the coro finishes → gen dropped (cancellation propagates).
//!
//! Hazards respected: no `Bound<'py>`/`Python` token crosses the channel or any
//! `.await`; the async-gen `Py<PyAny>` is `Send`+`Sync`; the channel item type
//! is `Result<Frame<Bytes>, io::Error>` (all `Send`). `next_chunk` (the old
//! per-chunk pull) is retained ONLY for the in-process `_drive_stream` test.
use bytes::Bytes;
use http_body_util::StreamBody;
use hyper::body::Frame;
use pyo3::exceptions::PyStopAsyncIteration;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};
use pyo3_async_runtimes::TaskLocals;
use std::io;
use std::sync::OnceLock;
use tokio::sync::mpsc::Sender;

/// Extract the wire bytes for one yielded chunk. `bytes`/`str` pass through
/// (str → utf-8); a `lancelot.stream.Event` (or anything with `render()`)
/// uses its render(); everything else is serialized via the pydantic-core
/// JSON seam + a trailing newline (JSON-lines, matching the ASGI seam's
/// `application/jsonl` auto-wrap).
fn chunk_to_bytes(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    if let Ok(b) = obj.cast::<PyBytes>() {
        return Ok(b.as_bytes().to_vec());
    }
    if let Ok(s) = obj.cast::<PyString>() {
        return Ok(s.to_str()?.as_bytes().to_vec());
    }
    if obj.hasattr("render")? {
        let r = obj.call_method0("render")?;
        if let Ok(b) = r.cast::<PyBytes>() {
            return Ok(b.as_bytes().to_vec());
        }
        if let Ok(s) = r.cast::<PyString>() {
            return Ok(s.to_str()?.as_bytes().to_vec());
        }
    }
    // Fallback: JSON-encode the object + newline (JSON-lines).
    let mut v = crate::serialize::to_json_bytes(py, obj)?;
    v.push(b'\n');
    Ok(v)
}

/// Obtain the async-iterator from a handler result. A handler may return the
/// async generator directly (it IS its own `__aiter__`) or a StreamingResponse-
/// like object exposing `__aiter__`. Returns None if `obj` is not async-iterable.
///
/// A plain coroutine (`async def` without `yield`) exposes `__await__` but
/// NOT `__anext__`/`__aiter__`, so it correctly returns None and falls through
/// to the regular await path (audit #6 — distinction verified).
pub fn as_async_iter(obj: &Bound<'_, PyAny>) -> Option<Py<PyAny>> {
    if obj.hasattr("__anext__").unwrap_or(false) {
        return Some(obj.clone().unbind());
    }
    if obj.hasattr("__aiter__").unwrap_or(false) {
        if let Ok(it) = obj.call_method0("__aiter__") {
            return Some(it.unbind());
        }
    }
    None
}

/// The response content-type for a stream. A StreamingResponse-like result
/// object may carry a `media_type` (e.g. "text/event-stream" for SSE Event
/// streams); otherwise default to JSON-lines (audit #4 — don't force SSE
/// bytes under an application/jsonl header).
///
/// SECURITY (m06-error-handling): `media_type` is user-controlled. Strip CR/
/// LF/control bytes + reject non-ASCII so it can never (a) inject a response
/// header (CRLF) nor (b) make hyper's `HeaderValue` parse fail and panic the
/// `.unwrap()` in the connection task. Falls back to the JSON-lines default
/// if the cleaned value is empty/invalid.
pub fn stream_media_type(obj: &Bound<'_, PyAny>) -> String {
    const DEFAULT: &str = "application/jsonl";
    if let Ok(mt) = obj.getattr("media_type") {
        if let Ok(s) = mt.extract::<String>() {
            let cleaned: String = s
                .chars()
                .filter(|c| c.is_ascii() && !c.is_ascii_control())
                .collect();
            if !cleaned.is_empty() {
                return cleaned;
            }
        }
    }
    DEFAULT.to_string()
}

/// Pull the next chunk: bridge `gen.__anext__()` to a Rust future (GIL held,
/// sync), drop the GIL across `.await`, then extract bytes (GIL held, sync).
/// Returns Ok(Some(bytes)) for a chunk, Ok(None) on StopAsyncIteration (clean
/// end), Err for any other Python exception.
async fn next_chunk(locals: &TaskLocals, gen: &Py<PyAny>) -> PyResult<Option<Vec<u8>>> {
    // Scope 1 (GIL, sync — no .await): bridge the coroutine.
    let fut = crate::gil::with_gil(|py| -> PyResult<_> {
        let coro = gen.bind(py).call_method0("__anext__")?;
        pyo3_async_runtimes::into_future_with_locals(locals, coro)
    })?;
    // GIL DROPPED across the await — other tasks/requests progress.
    match fut.await {
        Ok(obj) => {
            // Scope 3 (GIL, sync): extract bytes.
            let bytes = crate::gil::with_gil(|py| chunk_to_bytes(py, obj.bind(py)))?;
            Ok(Some(bytes))
        }
        Err(e) => {
            // StopAsyncIteration → clean end; anything else → propagate.
            let is_stop = crate::gil::with_gil(|py| e.is_instance_of::<PyStopAsyncIteration>(py));
            if is_stop {
                Ok(None)
            } else {
                Err(e)
            }
        }
    }
}

/// A Rust→Python sink the on-loop drain coroutine pushes each chunk into. The
/// drain coro runs ON the asyncio-loop thread, so `try_push` is a SAME-THREAD
/// synchronous channel send — no per-chunk `call_soon_threadsafe` cross-thread
/// hop (the measured ~0.8 ms/chunk cost of the old per-`__anext__` bridge that
/// made streaming a 16-26x cliff, see benchmarks/).
///
/// Backpressure is preserved (bounded channel, depth `stream_buffer()`): on a
/// full channel `try_push` returns False and the drain coro `await`s `await_capacity()` —
/// an event-driven, GIL-free wait that costs ONE cross-thread hop but ONLY when
/// the consumer is slow (rare). The fast path (channel not full) stays fully
/// synchronous, which is where the throughput win lives.
#[pyclass]
struct ChunkSink {
    tx: Option<Sender<Result<Frame<Bytes>, io::Error>>>,
    locals: TaskLocals,
}

#[pymethods]
impl ChunkSink {
    /// Fast path: one yielded chunk → wire bytes → bounded `try_send`.
    /// Returns True iff the chunk was sent. False means "not sent" for EITHER
    /// reason (full OR closed); the caller then `await`s `await_capacity`, which
    /// disambiguates (full → wait+retry; closed → stop). On Full the bytes are
    /// dropped and recomputed on the retry with the same chunk (rare; no loss).
    fn try_push(&self, obj: &Bound<'_, PyAny>) -> PyResult<bool> {
        let bytes = chunk_to_bytes(obj.py(), obj)?;
        match &self.tx {
            Some(tx) => Ok(tx.try_send(Ok(Frame::data(Bytes::from(bytes)))).is_ok()),
            None => Ok(false),
        }
    }

    /// Event-driven backpressure wait (returns a Python awaitable). `reserve()`
    /// resolves immediately if there's capacity, parks until the consumer drains
    /// otherwise; the permit is dropped (capacity signal only — the retrying
    /// `try_push` does the real send). RESULT: True = capacity available, retry;
    /// **False = channel closed (client disconnected) → the drain coro must
    /// STOP** (the cancellation path: without it the generator would keep being
    /// driven after the client is gone). Disconnect is observed at the next
    /// `try_push`, so an in-flight generator runs for AT MOST one more chunk,
    /// then the coro returns and the gen is left suspended. GIL is NOT held
    /// across the await — it's a Rust future on the runtime.
    fn await_capacity<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let tx = self.tx.clone();
        pyo3_async_runtimes::tokio::future_into_py_with_locals(
            py,
            self.locals.clone(),
            async move {
                Ok(match tx {
                    Some(tx) => tx.reserve().await.is_ok(),
                    None => false,
                })
            },
        )
    }

    /// Handler raised mid-stream: the 200 + headers are already on the wire, so
    /// emit an error frame (best-effort) and let the body truncate (Starlette
    /// parity — it logs and closes).
    fn fail(&self, _msg: &str) {
        if let Some(tx) = &self.tx {
            let _ = tx.try_send(Err(io::Error::other("stream handler error")));
        }
    }

    /// Stream finished → drop the sender → receiver EOFs → body ends.
    fn close(&mut self) {
        self.tx.take();
    }
}

/// Bounded stream-channel depth (chunks buffered before backpressure). Bigger =
/// fewer `await_capacity` waits under fast producers (more throughput), at the
/// cost of up to `depth × chunk_size` buffered per in-flight stream. Tunable via
/// `LANCELOT_STREAM_BUFFER` (clamped [1, 4096]); default 256 — tuned so small
/// (token-stream) chunks rarely block while keeping memory bounded.
fn stream_buffer() -> usize {
    static N: OnceLock<usize> = OnceLock::new();
    *N.get_or_init(|| {
        std::env::var("LANCELOT_STREAM_BUFFER")
            .ok()
            .and_then(|s| s.trim().parse::<usize>().ok())
            .filter(|&n| n >= 1)
            .unwrap_or(256)
            .min(4096)
    })
}

/// The on-loop drain coroutine, compiled once. `async for` runs on the asyncio
/// loop; each `sink.try_push` is same-thread. The inner `while` restores
/// backpressure: a full channel → `await sink.await_capacity()` → retry.
/// `finally: sink.close()` guarantees the body EOFs even on cancellation.
fn drainer(py: Python<'_>) -> PyResult<Py<PyAny>> {
    static D: OnceLock<Py<PyAny>> = OnceLock::new();
    if let Some(f) = D.get() {
        return Ok(f.clone_ref(py));
    }
    let code = cr#"
async def _drain(gen, sink):
    try:
        async for chunk in gen:
            while not sink.try_push(chunk):
                # full -> await_capacity True (retry); client gone -> False (stop,
                # leaving the generator suspended -> no runaway generation).
                if not await sink.await_capacity():
                    return
    except BaseException as e:
        try:
            sink.fail(repr(e))
        except Exception:
            pass
    finally:
        sink.close()
"#;
    let module = PyModule::from_code(py, code, c"_lc_drain.py", c"_lc_drain")?;
    let f = module.getattr("_drain")?.unbind();
    let _ = D.set(f.clone_ref(py)); // race-benign: equivalent fn either way
    Ok(f)
}

/// The hyper streaming body for a Python async-gen. R3 re-architecture: the
/// generator is drained by a coroutine running ON the asyncio loop (ONE bridge
/// crossing for the whole stream via `into_future_with_locals`), pushing chunks
/// into a BOUNDED channel same-thread — instead of the old design's one
/// cross-thread `call_soon_threadsafe` round-trip PER `__anext__` (16-26x
/// throughput cliff, see benchmarks/). Backpressure is preserved by the bounded
/// channel + the drain coro's `await_capacity` retry (see `ChunkSink`). Errors
/// mid-stream truncate the body (200 already committed).
///
/// `shutdown`: on SIGTERM the drive task stops awaiting the drain future; the
/// loop closing tears the coro down (its `finally` drops the sink → body EOF).
pub fn stream_body(
    locals: TaskLocals,
    gen: Py<PyAny>,
    shutdown: std::sync::Arc<tokio::sync::Notify>,
) -> StreamBody<tokio_stream::wrappers::ReceiverStream<Result<Frame<Bytes>, io::Error>>> {
    use futures::FutureExt;
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<Frame<Bytes>, io::Error>>(stream_buffer());
    // Scope (GIL, sync): build the sink + drain coroutine and bridge it ONCE.
    let driven = crate::gil::with_gil(|py| -> PyResult<_> {
        let sink = Py::new(
            py,
            ChunkSink {
                tx: Some(tx),
                locals: locals.clone(),
            },
        )?;
        let coro = drainer(py)?.bind(py).call1((gen.bind(py), sink))?;
        pyo3_async_runtimes::into_future_with_locals(&locals, coro)
    });
    if let Ok(fut) = driven {
        tokio::spawn(async move {
            // Drive the whole-stream drain to completion. A panic inside the
            // bridge becomes a dropped future (no UB / silent task abort).
            tokio::select! {
                biased;
                _ = shutdown.notified() => {}
                _ = std::panic::AssertUnwindSafe(fut).catch_unwind() => {}
            }
            // On either path the sink (owned by the coro) is eventually dropped
            // → its sender drops → rx EOFs.
        });
    }
    // (Err path: sink/tx dropped in the closure → rx EOFs → empty stream.)
    StreamBody::new(tokio_stream::wrappers::ReceiverStream::new(rx))
}

/// In-process test helper (mirrors dispatch::_drive): call `handler()` with no
/// request, pump the returned async-gen fully, return the chunk byte-strings.
/// Exercises the GIL choreography + StopAsyncIteration path without standing up
/// an HTTP server.
#[pyfunction]
pub fn _drive_stream(
    py: Python<'_>,
    fi: &crate::ffi::fninfo::FnInfo,
) -> PyResult<Vec<Py<PyBytes>>> {
    let handler = fi.handler.clone_ref(py);
    // RAII loop guard (m12-lifecycle) — closed on every exit path incl. run?.
    let guard = crate::gil::EventLoop::new(
        py.import("asyncio")?
            .call_method0("new_event_loop")?
            .unbind(),
    );
    let event_loop = guard.bind(py);
    let locals = TaskLocals::new(event_loop.clone()).copy_context(py)?;

    let driver = {
        let locals = locals.clone();
        async move {
            // Build the async-gen by calling the handler (GIL, sync).
            let gen = crate::gil::with_gil(|py| -> PyResult<Py<PyAny>> {
                let result = handler.bind(py).call0()?;
                as_async_iter(&result).ok_or_else(|| {
                    pyo3::exceptions::PyTypeError::new_err(
                        "handler did not return an async iterator",
                    )
                })
            })?;
            let mut out: Vec<Vec<u8>> = Vec::new();
            while let Some(b) = next_chunk(&locals, &gen).await? {
                out.push(b);
            }
            Ok::<_, PyErr>(out)
        }
    };

    // `guard` closes the loop in Drop on every exit path (incl. `run?`).
    let run = pyo3_async_runtimes::tokio::run_until_complete(event_loop, driver);
    let chunks = run?;
    Ok(chunks
        .into_iter()
        .map(|b| PyBytes::new(py, &b).unbind())
        .collect())
}
