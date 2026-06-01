//! M18 R1 — GIL-releasing gzip encode (Python ASGI seam only).
//!
//! This is the framework's FIRST explicit GIL-release call site (`py.detach`).
//! Lancelot otherwise releases the GIL *structurally* — every `.await` sits
//! outside a `with_gil` scope, so there is normally no GIL to drop at a yield
//! point (see `src/gil.rs:27-30`, which is why no `detach` wrapper exists).
//! Here we deliberately drop the GIL around a CPU-bound zlib pass so the caller
//! can run it on a thread-pool thread (`loop.run_in_executor`) and let OTHER
//! coroutines on the single uvloop event loop make progress while this encode
//! runs. Calling this INLINE on the loop thread releases the GIL to nobody and
//! yields no concurrency win — the off-loop hop in `lancelot/compress.py` is
//! mandatory and load-bearing.
//!
//! Codec: `libdeflater` (libdeflate bindings) — single-shot, `gzip_compress_bound`
//! gives one pre-sized allocation, ~2-3x zlib per byte. It's a Rust-side dep
//! (never enters `import lancelot`'s Python runtime), so cardinal-rule-3
//! (pydantic-only runtime) stays clean.
use libdeflater::{CompressionLvl, Compressor};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// gzip-compress `data` at `level` (libdeflate range 1-12), releasing the GIL
/// across the compression pass. Returns the gzip-framed bytes.
#[pyfunction]
pub fn gzip_encode(py: Python<'_>, data: &[u8], level: u8) -> PyResult<Py<PyBytes>> {
    let lvl = CompressionLvl::new(level as i32)
        .map_err(|e| PyValueError::new_err(format!("invalid gzip level {level}: {e:?}")))?;
    // GIL released here. `data: &[u8]` borrows the caller's live `bytes` object,
    // whose reference is held alive by the Python caller across this whole FFI
    // call (pyo3 only converts the argument; it does not drop the source). The
    // closure neither touches a `Python` token nor calls any `Py*` API while the
    // GIL is dropped, and it copies the result OUT into an owned `Vec` before we
    // re-attach to build the PyBytes — so the borrowed slice cannot dangle.
    let compressed: Result<Vec<u8>, _> = py.detach(|| {
        let mut c = Compressor::new(lvl);
        let mut out = vec![0u8; c.gzip_compress_bound(data.len())];
        c.gzip_compress(data, &mut out).map(|n| {
            out.truncate(n);
            out
        })
    });
    let out = compressed.map_err(|e| PyValueError::new_err(format!("gzip compress failed: {e:?}")))?;
    Ok(PyBytes::new(py, &out).unbind())
}
