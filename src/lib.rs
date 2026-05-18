use pyo3::prelude::*;

#[pyfunction]
fn _ping() -> &'static str {
    "lancelot-m0"
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(_ping, m)?)?;
    Ok(())
}
