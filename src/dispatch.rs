//! The GIL choreography. THREE `with_gil` scopes, none containing `.await`:
//!  (1) build the coroutine + bridge it to a Send Rust future
//!  (2) [GIL DROPPED] await the bridged future (Python's asyncio loop runs it;
//!      the GIL is free here — other requests' handlers progress)
//!  (3) one-pass serialize via the pydantic-core seam.
//!
//! Deviation from the plan's literal Task 8 code (necessary — see the review
//! agent's "highest-risk" flag): the plan used `pyo3_async_runtimes::tokio::
//! into_future(coro)` + `py.allow_threads` + a hand-rolled `rt.block_on`. In
//! pyo3-async-runtimes 0.27 `into_future` resolves task-locals via
//! `asyncio.get_running_loop`, which FAILS when driven from sync Python (pytest)
//! with no running loop. The correct API is `into_future_with_locals(&locals,
//! coro)` against an explicit `asyncio.new_event_loop()`, driven by
//! `tokio::run_until_complete(event_loop, driver)` (which spins the Python loop
//! AND the tokio runtime together). The GIL-released-across-await invariant is
//! unchanged and is what the concurrency test proves.
use crate::ffi::fninfo::FnInfo;
use crate::ffi::request::Request;
use crate::serialize::to_json_bytes;
use bytes::Bytes;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyo3_async_runtimes::TaskLocals;

/// The Send, Rust-owned slice of a request that crosses the `.await` boundary
/// (M3-R2). All owned values (String / Vec / Bytes) — no GIL-bound token, no
/// borrow of the hyper request — so the dispatch future stays `Send` and
/// nothing pins the GIL across the handler await.
pub struct ReqParts {
    pub method: String,
    pub path: String,
    pub query_string: String,
    pub headers: Vec<(String, String)>,
    pub body: Bytes,
}

/// Per-request choreography. `handler`/`parts` are `Send` Rust-owned values;
/// nothing GIL-bound (`Python`/`Bound`) crosses the `.await`.
pub async fn call_async(
    locals: TaskLocals,
    handler: Py<PyAny>,
    parts: ReqParts,
) -> PyResult<Bytes> {
    // Scope 1: build the coroutine and bridge it to a Rust future. The bridge
    // (`call_soon_threadsafe`) is synchronous — no `.await` in this GIL scope.
    // R2: the REAL method/path/query/headers are moved into the Request here
    // (was hardcoded "POST"/"/echo" in M0).
    //
    // A SYNC handler (`def h(request): return ...`) returns a non-awaitable
    // value whose body ALREADY ran during `call1`; serialize it here (GIL held)
    // and short-circuit — same `Ready` reasoning as `build_result`, mirrored so
    // the `_drive` test path also accepts sync handlers. `Ok` carries the
    // finished bytes; `Err` carries the future to bridge+await.
    let bridged = crate::gil::with_gil(|py| -> PyResult<Result<Bytes, _>> {
        let req = build_request(py, parts)?;
        let result = handler.bind(py).call1((req,))?;
        if !result.hasattr(pyo3::intern!(py, "__await__"))? {
            return Ok(Ok(to_json_bytes(py, &result)?));
        }
        Ok(Err(pyo3_async_runtimes::into_future_with_locals(
            &locals, result,
        )?))
    })?;
    let fut = match bridged {
        Ok(bytes) => return Ok(bytes), // sync handler: already serialized
        Err(fut) => fut,
    };

    // GIL is NOT held here. The asyncio loop runs the coroutine; other tasks run.
    let result: Py<PyAny> = fut.await?;

    // Scope 3: one-pass serialize (pydantic-core).
    crate::gil::with_gil(|py| to_json_bytes(py, result.bind(py)))
}

/// Build the lazy Request pyclass from the owned parts (GIL held).
fn build_request<'py>(py: Python<'py>, parts: ReqParts) -> PyResult<Bound<'py, Request>> {
    Bound::new(
        py,
        Request::new(
            parts.method,
            parts.path,
            parts.query_string,
            parts.headers,
            parts.body,
        ),
    )
}

/// The Send future that resolves a regular handler coroutine to its result.
pub type ResultFuture =
    std::pin::Pin<Box<dyn std::future::Future<Output = PyResult<Py<PyAny>>> + Send>>;

/// What a handler call produced: a streaming async-gen (R3), a regular
/// coroutine future, or an already-serialized SYNC result. Detection happens in
/// ONE GIL scope (build_result) so the hot path branches without a second
/// introspection.
pub enum Dispatched {
    /// `async def gen(): yield ...` — the async-iterator object to pump, plus
    /// the response content-type (SSE vs JSON-lines; audit #4).
    Stream(Py<PyAny>, String),
    /// A regular awaitable (`async def` handler's coroutine); await it (GIL
    /// dropped) then serialize.
    Value(ResultFuture),
    /// A SYNC handler (`def h(request): return ...`) — its body already ran
    /// INLINE during `call1`, so the return value is NOT awaitable. Serialize
    /// it RIGHT THERE in the GIL scope (no bridge, no `ensure_future`, no
    /// `.await`) and carry the finished bytes. The old code fed this value to
    /// `into_future_with_locals` → `asyncio.ensure_future(<value>)` → TypeError
    /// → 500. No threadpool is needed: the handler already completed.
    Ready(Bytes),
}

/// Scope 1 (GIL held): build the Request, call the handler, and classify the
/// result. An async-gen result short-circuits to streaming WITHOUT bridging a
/// coroutine future; a SYNC handler's (non-awaitable) return value is serialized
/// in-scope to `Ready(Bytes)`; a regular `async def` coroutine is bridged to a
/// Send `ResultFuture`.
pub fn build_result(
    py: Python<'_>,
    locals: &TaskLocals,
    handler: Py<PyAny>,
    parts: ReqParts,
) -> PyResult<Dispatched> {
    let req = build_request(py, parts)?;
    let result = handler.bind(py).call1((req,))?;
    if let Some(gen) = crate::stream::as_async_iter(&result) {
        let ct = crate::stream::stream_media_type(&result);
        return Ok(Dispatched::Stream(gen, ct));
    }
    // Hot-path discipline: ONE cheap interned-attr check, in the EXISTING GIL
    // scope (Scope 1) — no new GIL acquire, no `.await`. A coroutine (`async
    // def`) defines `__await__`; a sync handler's plain return value (dict /
    // list / model / …) does not. Branch on that:
    //  - awaitable → keep the proven bridge (into_future → await GIL-dropped).
    //  - NOT awaitable → the sync body ALREADY ran during `call1`; serialize
    //    its result here (GIL held; `to_json_bytes` maps Err→PyErr, no panic
    //    crosses the FFI seam) and skip the bridge entirely.
    if !result.hasattr(pyo3::intern!(py, "__await__"))? {
        let json = to_json_bytes(py, &result)?;
        return Ok(Dispatched::Ready(json));
    }
    let fut = pyo3_async_runtimes::into_future_with_locals(locals, result)?;
    Ok(Dispatched::Value(Box::pin(fut)))
}

/// Drop the GIL across the awaitable, then serialize the result (Scope 3).
pub async fn await_and_serialize(fut: ResultFuture) -> PyResult<Bytes> {
    let result: Py<PyAny> = fut.await?;
    crate::gil::with_gil(|py| to_json_bytes(py, result.bind(py)))
}

/// Drive `n` concurrent dispatches on a fresh asyncio loop + the tokio runtime,
/// returning `(serialized_results, wall_seconds)`. If the GIL were held across
/// the handler `await`, `n * 0.1s` would serialize (~5s for n=50); released, it
/// stays ~0.1–0.3s — the M0 DoD assertion #4.
#[pyfunction]
pub fn _drive(
    py: Python<'_>,
    fi: &FnInfo,
    body: &[u8],
    n: usize,
) -> PyResult<(Vec<Py<PyBytes>>, f64)> {
    // Scope 1 (GIL held via the inbound `py`): handler clones + task-locals.
    // Build the loop LAST so a setup failure above can't strand an unclosed
    // loop (audit #4 — the loop owns a kqueue + self-pipe fd pair).
    let handlers: Vec<Py<PyAny>> = (0..n).map(|_| fi.handler.clone_ref(py)).collect();
    let body = Bytes::copy_from_slice(body);
    // RAII: the loop is closed on EVERY exit path by the guard's Drop
    // (m12-lifecycle) — no hand-rolled close-on-each-path.
    // M7 T4.13: intern hot-path attr names. `new_event_loop` is called
    // ONCE per dispatch (cold-ish) but the asyncio symbol IS used per-
    // request via the dispatch task; cheap to intern at the call site.
    let guard = crate::gil::EventLoop::new(
        py.import(pyo3::intern!(py, "asyncio"))?
            .call_method0(pyo3::intern!(py, "new_event_loop"))?
            .unbind(),
    );
    let event_loop = guard.bind(py);
    let locals = TaskLocals::new(event_loop.clone()).copy_context(py)?;

    let driver = {
        let locals = locals.clone();
        async move {
            let t0 = std::time::Instant::now();
            let mut set = tokio::task::JoinSet::new();
            for h in handlers {
                let parts = ReqParts {
                    method: "POST".into(),
                    path: "/echo".into(),
                    query_string: String::new(),
                    headers: Vec::new(),
                    body: body.clone(),
                };
                set.spawn(call_async(locals.clone(), h, parts));
            }
            let mut out: Vec<Bytes> = Vec::with_capacity(set.len());
            while let Some(joined) = set.join_next().await {
                // A panicked dispatch task maps to a PyErr (audit #6) instead
                // of `.expect()` aborting the whole JoinSet.
                match joined {
                    Ok(r) => out.push(r?),
                    Err(e) => {
                        // M7 T4.12: error includes Rust file:line so the
                        // Python traceback names both Python + Rust frames.
                        return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                            "dispatch task failed at {}:{}: {e}",
                            file!(), line!()
                        )))
                    }
                }
            }
            Ok::<_, PyErr>((out, t0.elapsed().as_secs_f64()))
        }
    };

    // Runs the Python event loop AND the tokio runtime until `driver` resolves;
    // manages the GIL internally (released while the loop selects / tasks await).
    // `guard` closes the loop in Drop on every exit path — including the `run?`
    // early-return below (audit #4 / m12-lifecycle).
    let run = pyo3_async_runtimes::tokio::run_until_complete(event_loop, driver);
    let (results, wall): (Vec<Bytes>, f64) = run?;

    // Scope 3 (GIL): hand the serialized bytes back to Python.
    let py_results = results
        .into_iter()
        .map(|b| PyBytes::new(py, &b).unbind())
        .collect();
    Ok((py_results, wall))
}
