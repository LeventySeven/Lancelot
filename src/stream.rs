//! M3-R3 — the streaming Rust pump.
//!
//! A Python async generator (`async def gen(): yield ...`) is pulled chunk by
//! chunk and written to a hyper chunked body WITHOUT holding the GIL across any
//! `.await` and WITHOUT a per-chunk thread spawn (Robyn's documented defect,
//! deep/04:23 — Robyn spawns a thread + reacquires the GIL per token).
//!
//! Design (m07-concurrency skill — Web-API / message-passing / bounded
//! backpressure):
//!  - ONE long-lived tokio task pulls from the async-gen. Per chunk: a
//!    `with_gil` SYNC scope bridges `gen.__anext__()` to a Send Rust future
//!    (`into_future_with_locals`), then the GIL is DROPPED across `fut.await`.
//!  - The chunk bytes are extracted in a second short `with_gil` scope and sent
//!    over a BOUNDED mpsc channel (capacity 16). A slow client fills the channel
//!    → `send().await` parks the pull task → backpressure propagates all the way
//!    to the upstream LLM (no unbounded buffering).
//!  - StopAsyncIteration ends the stream cleanly (drop the sender → body EOF).
//!  - If the client disconnects, hyper drops the body → receiver drops →
//!    `send()` errors → the pull loop breaks → the gen is dropped (cancellation
//!    propagates; matches `request.cancelled`).
//!
//! Hazards respected (per the skill): no `Bound<'py>`/`Python` token crosses the
//! channel or the `.await`; the async-gen `Py<PyAny>` is `Send`+`Sync`; the
//! channel item type is `Result<Frame<Bytes>, io::Error>` (all `Send`).
use bytes::Bytes;
use http_body_util::StreamBody;
use hyper::body::Frame;
use pyo3::exceptions::PyStopAsyncIteration;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};
use pyo3_async_runtimes::TaskLocals;
use std::io;

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
pub fn stream_media_type(obj: &Bound<'_, PyAny>) -> String {
    if let Ok(mt) = obj.getattr("media_type") {
        if let Ok(s) = mt.extract::<String>() {
            if !s.is_empty() {
                return s;
            }
        }
    }
    "application/jsonl".to_string()
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

/// The hyper streaming body for a Python async-gen. Spawns ONE pull task that
/// feeds a bounded channel; the returned StreamBody drains it. Errors mid-stream
/// terminate the body (the headers/200 are already committed by the caller).
///
/// `shutdown`: notified on graceful server shutdown (SIGTERM). The pull loop
/// selects on it so an in-flight stream stops promptly instead of awaiting a
/// coroutine on a soon-to-close event loop (audit #2). The whole pull loop is
/// `catch_unwind`-wrapped so a Rust panic in the pull/extract path terminates
/// the stream cleanly instead of a silent tokio task abort (audit #3 — parity
/// with the non-streaming `await_and_serialize` catch_unwind in service.rs).
pub fn stream_body(
    locals: TaskLocals,
    gen: Py<PyAny>,
    shutdown: std::sync::Arc<tokio::sync::Notify>,
) -> StreamBody<tokio_stream::wrappers::ReceiverStream<Result<Frame<Bytes>, io::Error>>> {
    use futures::FutureExt;
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<Frame<Bytes>, io::Error>>(16);
    tokio::spawn(async move {
        let pump = std::panic::AssertUnwindSafe(async {
            loop {
                let next = tokio::select! {
                    biased;
                    // Graceful shutdown wins — stop pulling on a loop that is
                    // about to close.
                    _ = shutdown.notified() => break,
                    n = next_chunk(&locals, &gen) => n,
                };
                match next {
                    Ok(Some(bytes)) => {
                        // Bounded send → backpressure. Err = receiver dropped
                        // (client gone) → stop pulling, drop the gen.
                        if tx
                            .send(Ok(Frame::data(Bytes::from(bytes))))
                            .await
                            .is_err()
                        {
                            break;
                        }
                    }
                    Ok(None) => break, // StopAsyncIteration → clean EOF
                    Err(_e) => {
                        // A handler exception mid-stream: the 200 + headers are
                        // already on the wire, so we can't change the status.
                        // End the body (truncated stream — same as
                        // FastAPI/Starlette, which log and close).
                        let _ = tx
                            .send(Err(io::Error::other("stream handler error")))
                            .await;
                        break;
                    }
                }
            }
        });
        // A panic in the pull/extract path → terminate the stream, not a
        // silent task death.
        if pump.catch_unwind().await.is_err() {
            let _ = tx.send(Err(io::Error::other("stream pump panicked"))).await;
        }
        // gen drops here (under the task's scope); a final GIL acquire to
        // release the Py ref happens in Py's Drop via pyo3's refcount path.
    });
    StreamBody::new(tokio_stream::wrappers::ReceiverStream::new(rx))
}

/// In-process test helper (mirrors dispatch::_drive): call `handler()` with no
/// request, pump the returned async-gen fully, return the chunk byte-strings.
/// Exercises the GIL choreography + StopAsyncIteration path without standing up
/// an HTTP server.
#[pyfunction]
pub fn _drive_stream(py: Python<'_>, fi: &crate::ffi::fninfo::FnInfo) -> PyResult<Vec<Py<PyBytes>>> {
    let handler = fi.handler.clone_ref(py);
    let event_loop = py.import("asyncio")?.call_method0("new_event_loop")?;
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

    let run = pyo3_async_runtimes::tokio::run_until_complete(event_loop.clone(), driver);
    let _ = event_loop.call_method0("close");
    let chunks = run?;
    Ok(chunks
        .into_iter()
        .map(|b| PyBytes::new(py, &b).unbind())
        .collect())
}
