use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// One-pass typed serialization: delegate to pydantic_core.to_json (Rust core,
/// bytes out, no intermediate dict, no second json.dumps). Falls back to stdlib
/// json only if pydantic_core is unavailable.
pub fn to_json_bytes(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    if let Ok(pc) = py.import("pydantic_core") {
        let out = pc.call_method1("to_json", (value,))?; // -> bytes
                                                         // pyo3 0.27.2 deprecates PyAnyMethods::downcast in favour of Bound::cast.
        return Ok(out.cast::<PyBytes>()?.as_bytes().to_vec());
    }
    let json = py.import("json")?;
    let s: String = json.call_method1("dumps", (value,))?.extract()?;
    Ok(s.into_bytes())
}

#[pyfunction]
pub fn _to_json<'py>(py: Python<'py>, value: Bound<'py, PyAny>) -> PyResult<Py<PyBytes>> {
    let bytes = to_json_bytes(py, &value)?;
    Ok(PyBytes::new(py, &bytes).unbind())
}
