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
use crate::serialize::{render_result, Rendered};
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
) -> PyResult<Rendered> {
    // Scope 1: build the coroutine and bridge it to a Rust future. The bridge
    // (`call_soon_threadsafe`) is synchronous — no `.await` in this GIL scope.
    // R2: the REAL method/path/query/headers are moved into the Request here
    // (was hardcoded "POST"/"/echo" in M0).
    //
    // A SYNC handler (`def h(request): return ...`) returns a non-awaitable
    // value whose body ALREADY ran during `call1`; render it here (GIL held)
    // and short-circuit — same `Ready` reasoning as `build_result`, mirrored so
    // the `_drive` test path also accepts sync handlers. `Ok` carries the
    // finished render; `Err` carries the future to bridge+await.
    let bridged = crate::gil::with_gil(|py| -> PyResult<Result<Rendered, _>> {
        let result = call_handler(py, handler.bind(py), parts)?;
        if !result.hasattr(pyo3::intern!(py, "__await__"))? {
            return Ok(Ok(render_result(py, &result)?));
        }
        Ok(Err(pyo3_async_runtimes::into_future_with_locals(
            &locals, result,
        )?))
    })?;
    let fut = match bridged {
        Ok(rendered) => return Ok(rendered), // sync handler: already rendered
        Err(fut) => fut,
    };

    // GIL is NOT held here. The asyncio loop runs the coroutine; other tasks run.
    let result: Py<PyAny> = fut.await?;

    // Scope 3: render the result (Response → status/headers/body; else JSON).
    crate::gil::with_gil(|py| render_result(py, result.bind(py)))
}

/// Call a handler with the right arity (GIL held). The native server's contract
/// is `handler(request)`, but the builtin probes (`/livez`, `/readyz`,
/// `/fleet/status`, `/openapi.json`, …) are registered as ZERO-arg functions
/// (`def _livez(): return JSONResponse(...)`) — calling those with one arg
/// raises `TypeError: takes 0 positional arguments but 1 was given`, which the
/// service layer maps to 500. (The ASGI seam side-steps this via full DI, which
/// the native path deliberately does NOT do — DI requires uvicorn.)
///
/// So: inspect the handler's positional-arg count ONCE (a single `__code__`
/// read — same cost regime as the adjacent `hasattr(__await__)` check) and call
/// `call0()` for a genuine zero-arg handler, `call1((req,))` otherwise. A
/// callable WITHOUT a `__code__` (builtin / callable object / functools.partial)
/// falls back to the established `call1((req,))` contract — the normal
/// user-handler shape. `req` is built lazily and only when actually passed, so a
/// zero-arg probe pays nothing for an unused Request.
fn call_handler<'py>(
    py: Python<'py>,
    handler: &Bound<'py, PyAny>,
    parts: ReqParts,
) -> PyResult<Bound<'py, PyAny>> {
    // Zero positional args ONLY when we can prove it via `__code__.co_argcount`
    // (a plain `def`/`async def`). Anything we can't introspect → pass the
    // Request (the default native contract). `co_argcount` excludes *args, so a
    // `def h(*a)` reports 0 but the call below would still pass req — which is
    // correct (it accepts it). We only skip the arg when argcount is provably 0
    // AND there's no *args.
    let wants_no_arg = (|| -> Option<bool> {
        let code = handler.getattr(pyo3::intern!(py, "__code__")).ok()?;
        let argcount: usize = code
            .getattr(pyo3::intern!(py, "co_argcount"))
            .ok()?
            .extract()
            .ok()?;
        // CO_VARARGS = 0x04: a `*args` function accepts the Request even with
        // co_argcount 0, so don't treat it as zero-arg.
        let flags: u32 = code
            .getattr(pyo3::intern!(py, "co_flags"))
            .ok()?
            .extract()
            .ok()?;
        let has_varargs = flags & 0x04 != 0;
        Some(argcount == 0 && !has_varargs)
    })()
    .unwrap_or(false);
    if wants_no_arg {
        handler.call0()
    } else {
        let req = build_request(py, parts)?;
        handler.call1((req,))
    }
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
    /// dropped) then render.
    Value(ResultFuture),
    /// A SYNC handler (`def h(request): return ...`) — its body already ran
    /// INLINE during `call1`, so the return value is NOT awaitable. Render
    /// it RIGHT THERE in the GIL scope (no bridge, no `ensure_future`, no
    /// `.await`) and carry the finished render. The old code fed this value to
    /// `into_future_with_locals` → `asyncio.ensure_future(<value>)` → TypeError
    /// → 500. No threadpool is needed: the handler already completed.
    Ready(Rendered),
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
    let result = call_handler(py, handler.bind(py), parts)?;
    if let Some(gen) = crate::stream::as_async_iter(&result) {
        let ct = crate::stream::stream_media_type(&result);
        return Ok(Dispatched::Stream(gen, ct));
    }
    // Hot-path discipline: ONE cheap interned-attr check, in the EXISTING GIL
    // scope (Scope 1) — no new GIL acquire, no `.await`. A coroutine (`async
    // def`) defines `__await__`; a sync handler's plain return value (dict /
    // list / model / …) does not. Branch on that:
    //  - awaitable → keep the proven bridge (into_future → await GIL-dropped).
    //  - NOT awaitable → the sync body ALREADY ran during `call_handler`; render
    //    its result here (GIL held; `render_result` maps Err→PyErr, no panic
    //    crosses the FFI seam) and skip the bridge entirely.
    if !result.hasattr(pyo3::intern!(py, "__await__"))? {
        let rendered = render_result(py, &result)?;
        return Ok(Dispatched::Ready(rendered));
    }
    let fut = pyo3_async_runtimes::into_future_with_locals(locals, result)?;
    Ok(Dispatched::Value(Box::pin(fut)))
}

/// Drop the GIL across the awaitable, then render the result (Scope 3) — a
/// returned `Response` carries status/content-type/headers, a plain value
/// becomes a 200 application/json body.
pub async fn await_and_serialize(fut: ResultFuture) -> PyResult<Rendered> {
    let result: Py<PyAny> = fut.await?;
    crate::gil::with_gil(|py| render_result(py, result.bind(py)))
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
                    // `_drive` is the in-process test helper: it asserts on the
                    // serialized BODY, so extract `.body` from the Rendered (the
                    // status/header carrier the production path now uses).
                    Ok(r) => out.push(r?.body),
                    Err(e) => {
                        // M7 T4.12: error includes Rust file:line so the
                        // Python traceback names both Python + Rust frames.
                        return Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                            "dispatch task failed at {}:{}: {e}",
                            file!(),
                            line!()
                        )));
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
