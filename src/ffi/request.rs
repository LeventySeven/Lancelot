use bytes::Bytes;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::OnceLock;

/// Lazy request: nothing is decoded until Python touches it. This object IS the
/// shared mutable request (do not rebuild it per phase — that breaks middleware
/// and the contextvars contract, per research deep/04).
#[pyclass]
pub struct Request {
    method: String,
    path: String,
    body: Bytes,
    json_cache: OnceLock<Py<PyAny>>,
}

impl Request {
    pub fn new(method: String, path: String, body: Bytes) -> Self {
        Self {
            method,
            path,
            body,
            json_cache: OnceLock::new(),
        }
    }
}

#[pymethods]
impl Request {
    #[getter]
    fn method(&self) -> &str {
        &self.method
    }

    #[getter]
    fn path(&self) -> &str {
        &self.path
    }

    fn json(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if let Some(v) = self.json_cache.get() {
            return Ok(v.clone_ref(py));
        }
        let json = py.import("json")?;
        let parsed = json
            .call_method1("loads", (PyBytes::new(py, &self.body),))?
            .unbind();
        let _ = self.json_cache.set(parsed.clone_ref(py));
        Ok(parsed)
    }
}

/// Test-only constructor (gated; removed when service.rs builds Requests for real).
#[pyfunction]
pub fn _test_make_request(method: &[u8], path: &[u8], body: &[u8]) -> Request {
    Request::new(
        String::from_utf8_lossy(method).into_owned(),
        String::from_utf8_lossy(path).into_owned(),
        Bytes::copy_from_slice(body),
    )
}
