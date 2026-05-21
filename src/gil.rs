//! The core invariant: a GIL scope is a SYNC closure. `.await` inside it is a
//! compile error by construction (you cannot `.await` in a non-async closure).
//!
//! This is the load-bearing safety property of the whole framework: the GIL is
//! the process-wide interpreter lock; holding it across `.await` would freeze
//! every other task (and, under free-threading, is a data-race surface). The
//! sync-`FnOnce` signature makes "await while holding the GIL" a *type error* at
//! zero runtime cost — proven by the `tests/ui/gil_await_fail.rs` trybuild
//! compile-fail fixture.
//!
//! API note (deviation from the plan's literal code, deliberate): the plan
//! expected pyo3 0.27 to still expose `Python::with_gil` / `allow_threads` and
//! to rename to `attach` / `detach` only at the 0.28 bump. pyo3 0.27.2 **already
//! deprecates** `with_gil`/`allow_threads`, so `with_gil` wraps `attach` now —
//! this matches the plan's stated "written 0.28-clean" intent and keeps the
//! build warning-free (an M0 Definition-of-Done requirement).
use pyo3::prelude::*;
use pyo3::Python;

/// Run `f` while holding the GIL. `f` is a synchronous `FnOnce`, so `.await`
/// inside it does not compile — the invariant is enforced by the type system.
#[inline]
pub fn with_gil<R>(f: impl for<'py> FnOnce(Python<'py>) -> R) -> R {
    Python::attach(f)
}

// (No `detach` wrapper: the GIL is released *structurally* — every `.await`
// sits OUTSIDE a `with_gil` scope, so there is no GIL to drop at the await
// point. An explicit `py.detach` marker would imply a release pattern this
// crate doesn't use, so it's intentionally absent.)

/// RAII guard for a freshly-created asyncio event loop (m12-lifecycle skill:
/// manual cleanup is the anti-pattern → use Drop). A loop owns a kqueue +
/// self-pipe fd pair; forgetting `close()` leaks them. Holding the loop in
/// this guard guarantees `close()` runs on EVERY scope exit — including an
/// early `?` between loop-creation and the run — exactly once, under the GIL.
///
/// Per-call paths (`_drive`, `_drive_stream`) build a fresh loop each call, so
/// the leak there accumulates; this guard closes it deterministically. (The
/// server's loop is process-lifetime / TaskLocals-owned and closed once.)
pub struct EventLoop {
    // Option so Drop can `take()` the Py without moving out of &mut self (E0509).
    obj: Option<pyo3::Py<pyo3::PyAny>>,
}

impl EventLoop {
    /// Wrap an owned loop object (from `asyncio.new_event_loop()`).
    pub fn new(obj: pyo3::Py<pyo3::PyAny>) -> Self {
        Self { obj: Some(obj) }
    }

    /// Borrow the loop as an owned `Bound` for passing to
    /// `run_until_complete` / `TaskLocals::new` (both want a Bound).
    pub fn bind<'py>(&self, py: Python<'py>) -> pyo3::Bound<'py, pyo3::PyAny> {
        // SAFETY of unwrap: `obj` is Some for the guard's whole lifetime;
        // only Drop takes it, after which `bind` is never called.
        self.obj
            .as_ref()
            .expect("event loop already taken")
            .bind(py)
            .clone()
    }
}

impl Drop for EventLoop {
    fn drop(&mut self) {
        if let Some(lp) = self.obj.take() {
            // Re-attach the GIL (reentrant — safe whether or not a caller
            // token is live) to close the loop, then the Py ref drops here
            // under the GIL. `close()` errors are ignored (idempotent /
            // best-effort cleanup — the loop has already finished running).
            with_gil(|py| {
                let _ = lp.bind(py).call_method0("close");
            });
        }
    }
}
