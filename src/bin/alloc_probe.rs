//! Counts Rust-heap allocations for ONE warm dispatch of a trivial handler.
//! Gate: <= 5 framework allocs (target 1-3). dhat wraps the *Rust* global
//! allocator, so CPython/pymalloc object churn is not counted — this measures
//! the framework's own Rust-side allocations on the hot path.
//!
//! Run: cargo run --bin alloc_probe --features dhat-heap --release
//!
//! Deviation from the plan's literal Task 11 (necessary, per the FFI-core
//! review carry-forward #1): the plan used deprecated
//! `prepare_freethreaded_python()` / `Python::with_gil` and the old 2-arg
//! `call_async(handler, body)` with a bare `rt.block_on`. The shipped API is
//! `Python::initialize()` + `gil::with_gil` + 3-arg
//! `call_async(TaskLocals, Py, Bytes)` driven by `run_until_complete` (the
//! coroutine only progresses while the asyncio loop runs). The loop is started
//! once OUTSIDE the measured region (after the warm call); the measured delta
//! is the steady per-request cost.
#[cfg(feature = "dhat-heap")]
#[allow(unused_imports)]
use lancelot::pyo3::prelude::*;

#[cfg(feature = "dhat-heap")]
#[global_allocator]
static ALLOC: dhat::Alloc = dhat::Alloc;

fn main() {
    #[cfg(feature = "dhat-heap")]
    {
        let _profiler = dhat::Profiler::builder().testing().build();
        lancelot::pyo3::Python::initialize();
        let body = bytes::Bytes::from_static(b"{\"a\":1}");

        let (h_warm, h_meas, locals, event_loop) =
            lancelot::pyo3::Python::attach(|py| -> lancelot::pyo3::PyResult<_> {
                let el = py.import("asyncio")?.call_method0("new_event_loop")?;
                let locals = pyo3_async_runtimes::TaskLocals::new(el.clone()).copy_context(py)?;
                let code =
                    std::ffi::CString::new("async def h(req):\n    return {'echo': req.json()}\n")
                        .unwrap();
                let m = lancelot::pyo3::types::PyModule::from_code(
                    py,
                    &code,
                    &std::ffi::CString::new("h.py").unwrap(),
                    &std::ffi::CString::new("h").unwrap(),
                )?;
                let h = m.getattr("h")?;
                Ok((h.clone().unbind(), h.unbind(), locals, el.unbind()))
            })
            .expect("probe setup");

        let driver = {
            let locals = locals.clone();
            let body = body.clone();
            async move {
                // Warm once (amortize one-time imports / first-call setup).
                let _ = lancelot::dispatch::call_async(locals.clone(), h_warm, body.clone()).await;
                let before = dhat::HeapStats::get();
                // The measured steady dispatch.
                let _ = lancelot::dispatch::call_async(locals.clone(), h_meas, body.clone()).await;
                let after = dhat::HeapStats::get();
                Ok::<_, lancelot::pyo3::PyErr>(
                    after.total_blocks.saturating_sub(before.total_blocks),
                )
            }
        };

        let delta = lancelot::pyo3::Python::attach(move |py| {
            let el = event_loop.into_bound(py);
            pyo3_async_runtimes::tokio::run_until_complete(el, driver)
        })
        .expect("probe run");

        println!("framework allocations for one request: {delta}");
        assert!(delta <= 5, "M0 alloc gate failed: {delta} > 5");
    }
    #[cfg(not(feature = "dhat-heap"))]
    eprintln!("run with --features dhat-heap");
}
