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
//! deprecates** `with_gil`/`allow_threads`, so we use `attach`/`detach` now —
//! this matches the plan's stated "written 0.28-clean" intent and keeps the
//! build warning-free (an M0 Definition-of-Done requirement).
use pyo3::Python;

/// Run `f` while holding the GIL. `f` is a synchronous `FnOnce`, so `.await`
/// inside it does not compile — the invariant is enforced by the type system.
#[inline]
pub fn with_gil<R>(f: impl for<'py> FnOnce(Python<'py>) -> R) -> R {
    Python::attach(f)
}

/// Explicit GIL-release marker for blocking sections (used by dispatch to drop
/// the GIL around the Python `await`). `R: Send` + the closure `: Send` satisfy
/// pyo3's `Ungil` bound — i.e. nothing GIL-bound may cross the release.
#[inline]
pub fn detach<R: Send>(py: Python<'_>, f: impl FnOnce() -> R + Send) -> R {
    py.detach(f)
}
