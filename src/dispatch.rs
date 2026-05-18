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

/// Per-request choreography. `handler`/`body` are `Send` Rust-owned values;
/// nothing GIL-bound (`Python`/`Bound`) crosses the `.await`.
pub async fn call_async(
    locals: TaskLocals,
    handler: Py<PyAny>,
    body: Bytes,
) -> PyResult<Vec<u8>> {
    // Scope 1: build the coroutine and bridge it to a Rust future. The bridge
    // (`call_soon_threadsafe`) is synchronous — no `.await` in this GIL scope.
    let fut = crate::gil::with_gil(|py| -> PyResult<_> {
        let req = Bound::new(py, Request::new("POST".into(), "/echo".into(), body))?;
        let coro = handler.bind(py).call1((req,))?;
        pyo3_async_runtimes::into_future_with_locals(&locals, coro)
    })?;

    // GIL is NOT held here. The asyncio loop runs the coroutine; other tasks run.
    let result: Py<PyAny> = fut.await?;

    // Scope 3: one-pass serialize (pydantic-core).
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
    // Scope 1 (GIL held via the inbound `py`): loop, task-locals, handler clones.
    let event_loop = py.import("asyncio")?.call_method0("new_event_loop")?;
    let locals = TaskLocals::new(event_loop.clone()).copy_context(py)?;
    let handlers: Vec<Py<PyAny>> = (0..n).map(|_| fi.handler.clone_ref(py)).collect();
    let body = Bytes::copy_from_slice(body);

    let driver = {
        let locals = locals.clone();
        async move {
            let t0 = std::time::Instant::now();
            let mut set = tokio::task::JoinSet::new();
            for h in handlers {
                set.spawn(call_async(locals.clone(), h, body.clone()));
            }
            let mut out: Vec<Vec<u8>> = Vec::with_capacity(set.len());
            while let Some(joined) = set.join_next().await {
                out.push(joined.expect("dispatch task panicked")?);
            }
            Ok::<_, PyErr>((out, t0.elapsed().as_secs_f64()))
        }
    };

    // Runs the Python event loop AND the tokio runtime until `driver` resolves;
    // manages the GIL internally (released while the loop selects / tasks await).
    let (results, wall): (Vec<Vec<u8>>, f64) =
        pyo3_async_runtimes::tokio::run_until_complete(event_loop, driver)?;

    // Scope 3 (GIL): hand the serialized bytes back to Python.
    let py_results = results
        .into_iter()
        .map(|b| PyBytes::new(py, &b).unbind())
        .collect();
    Ok((py_results, wall))
}
