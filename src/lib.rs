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

#[pyfunction]
fn _ping() -> &'static str {
    "lancelot-m0"
}

/// `routes`: list of (method, path, handler). Builds the router + a
/// long-lived asyncio loop/TaskLocals and serves until SIGTERM.
#[pyfunction]
#[pyo3(signature = (routes, host="127.0.0.1", port=8000))]
fn run(
    py: Python<'_>,
    routes: Vec<(String, String, Py<PyAny>)>,
    host: &str,
    port: u16,
) -> PyResult<()> {
    let event_loop = py.import("asyncio")?.call_method0("new_event_loop")?;
    let locals = pyo3_async_runtimes::TaskLocals::new(event_loop).copy_context(py)?;
    let mut router = crate::router::Router::new();
    let mut handlers = Vec::with_capacity(routes.len());
    for (i, (method, path, h)) in routes.into_iter().enumerate() {
        // Propagate a duplicate/malformed-route error as a clean Python
        // ValueError at startup instead of panicking across the FFI seam.
        router
            .add(&method, &path, i)
            .map_err(pyo3::exceptions::PyValueError::new_err)?;
        handlers.push(h);
    }
    crate::service::serve(
        py,
        crate::service::App {
            router,
            handlers,
            locals,
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
    m.add_function(wrap_pyfunction!(crate::ffi::request::_test_make_request, m)?)?;
    m.add_function(wrap_pyfunction!(crate::serialize::_to_json, m)?)?;
    m.add_function(wrap_pyfunction!(crate::dispatch::_drive, m)?)?;
    Ok(())
}
