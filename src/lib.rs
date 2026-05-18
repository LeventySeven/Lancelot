use pyo3::prelude::*;

// Re-export pyo3 so standalone entrypoints (trybuild fixtures, the Task 11
// alloc_probe bin) can initialize the embedded interpreter via a single
// consistent version path without taking a direct pyo3 dependency.
pub use pyo3;

pub mod gil;

#[pyfunction]
fn _ping() -> &'static str {
    "lancelot-m0"
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(_ping, m)?)?;
    Ok(())
}
