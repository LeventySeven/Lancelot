use pyo3::prelude::*;

// Re-export pyo3 so standalone entrypoints (trybuild fixtures, the Task 11
// alloc_probe bin) can initialize the embedded interpreter via a single
// consistent version path without taking a direct pyo3 dependency.
pub use pyo3;

pub mod cpu;
pub mod dispatch;
pub mod ffi;
pub mod gil;
pub mod router;
pub mod serialize;
pub mod service;
pub mod socket;
pub mod stream;

#[pyfunction]
fn _ping() -> &'static str {
    "lancelot-m0"
}

/// Build the embedded asyncio loop, preferring uvloop. uvloop is the SAME
/// C-based loop uvicorn uses to beat us on tiny one-shot responses (the one
/// conceded loss in the honest perf envelope); running our handler dispatch on
/// it shrinks the per-request asyncio overhead (call_soon_threadsafe / future
/// scheduling) the FFI bridge pays. Soft-dep — uvloop stays OUT of the runtime
/// deps (cardinal rule: pydantic-only); if it isn't installed we fall back to
/// stdlib asyncio. `LANCELOT_LOOP=asyncio` forces stdlib (escape hatch / A-B).
fn make_event_loop(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    let force_stdlib = std::env::var("LANCELOT_LOOP")
        .map(|v| v.eq_ignore_ascii_case("asyncio"))
        .unwrap_or(false);
    if !force_stdlib {
        if let Ok(uvloop) = py.import("uvloop") {
            if let Ok(loop_) = uvloop.call_method0("new_event_loop") {
                return Ok(loop_);
            }
        }
    }
    py.import("asyncio")?.call_method0("new_event_loop")
}

/// `routes`: list of (method, path, handler). `const_routes` (optional, trailing
/// keyword for back-compat): list of (method, path, body, content_type, status)
/// served as prebaked bytes straight from Rust — zero GIL/Request/handler (the
/// const-bytes tier; ideal for health probes + static responses). `on_startup` /
/// `on_shutdown` (optional): zero-arg Python callables returning a coroutine;
/// the server drives `on_startup`'s coroutine on its own loop BEFORE binding
/// (a raising startup -> refuse to serve) and `on_shutdown`'s AFTER draining,
/// so `App.run()` runs the ASGI lifespan protocol (M18 Task#7). Builds the
/// router + a long-lived asyncio loop/TaskLocals and serves until SIGTERM.
#[pyfunction]
#[pyo3(signature = (routes, host="127.0.0.1", port=8000, const_routes=None,
                    on_startup=None, on_shutdown=None))]
// The const_routes tuple shape (method, path, body, content_type, status) is a
// thin FFI DTO from Python — a named struct would just add ceremony at the seam.
#[allow(clippy::type_complexity)]
fn run(
    py: Python<'_>,
    routes: Vec<(String, String, Py<PyAny>)>,
    host: &str,
    port: u16,
    const_routes: Option<Vec<(String, String, Vec<u8>, String, u16)>>,
    on_startup: Option<Py<PyAny>>,
    on_shutdown: Option<Py<PyAny>>,
) -> PyResult<()> {
    let event_loop = make_event_loop(py)?;
    let locals = pyo3_async_runtimes::TaskLocals::new(event_loop).copy_context(py)?;
    let const_routes = const_routes.unwrap_or_default();
    let mut router = crate::router::Router::new();
    let mut handlers = Vec::with_capacity(routes.len() + const_routes.len());
    let mut const_bodies: Vec<Option<crate::service::Prebaked>> = Vec::new();
    let mut slot = 0usize;
    for (method, path, h) in routes {
        // M7 T4.12: registration errors include the Rust source location
        // so the operator's traceback names the file:line in addition to
        // the Python frame. Replaces the prior `PyValueError::new_err(e)`
        // (just the message). Done at the call site here rather than
        // a snafu macro — 9 ad-hoc sites total, all in this file/service.rs.
        router.add(&method, &path, slot).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "route registration failed at {}:{}: {} (method={}, path={})",
                file!(), line!(), e, method, path
            ))
        })?;
        handlers.push(h);
        const_bodies.push(None);
        slot += 1;
    }
    for (method, path, body, content_type, status) in const_routes {
        let pre = crate::service::Prebaked::new(status, &content_type, body)
            .map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!(
                    "const-route prebake failed at {}:{}: {} \
                     (method={}, path={}, content-type={}, status={})",
                    file!(), line!(), e, method, path, content_type, status
                ))
            })?;
        router.add(&method, &path, slot).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "const-route registration failed at {}:{}: {} (method={}, path={})",
                file!(), line!(), e, method, path
            ))
        })?;
        handlers.push(py.None()); // placeholder; never called
        const_bodies.push(Some(pre));
        slot += 1;
    }
    crate::service::serve(
        py,
        crate::service::App {
            router,
            handlers,
            const_bodies,
            locals,
            shutdown: std::sync::Arc::new(tokio::sync::Notify::new()),
            on_startup,
            on_shutdown,
        },
        host,
        port,
    )
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // One process-wide multi-thread tokio runtime, cgroup-aware width, wired
    // into pyo3-async-runtimes so run_until_complete / into_future use it.
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(crate::cpu::worker_count())
        .enable_all()
        .build()
        .expect("build tokio runtime");
    pyo3_async_runtimes::tokio::init_with_runtime(Box::leak(Box::new(rt))).ok();

    m.add_function(wrap_pyfunction!(_ping, m)?)?;
    m.add_function(wrap_pyfunction!(run, m)?)?;
    m.add_class::<crate::ffi::FnInfo>()?;
    m.add_class::<crate::ffi::Request>()?;
    m.add_function(wrap_pyfunction!(
        crate::ffi::request::_test_make_request,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(crate::serialize::_to_json, m)?)?;
    m.add_function(wrap_pyfunction!(crate::dispatch::_drive, m)?)?;
    m.add_function(wrap_pyfunction!(crate::stream::_drive_stream, m)?)?;
    Ok(())
}
