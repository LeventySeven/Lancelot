use bytes::Bytes;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::sync::OnceLock;

/// Lazy request proxy (R2): nothing is decoded until Python touches it. This
/// object IS the shared mutable request (do not rebuild it per phase — that
/// breaks middleware and the contextvars contract, per research deep/04).
///
/// Threads the REAL method / path / raw query string / headers from the hyper
/// request (M0 hardcoded "POST" / "/echo" — fixed here). Header and query
/// parsing is lazy (only on first Python access) so a handler that never reads
/// them pays nothing — the Granian-style "throughput doubling/tripling" lever
/// (design/01:120) is exactly this no-up-front-decode property.
#[pyclass]
pub struct Request {
    method: String,
    path: String,
    query_string: String,
    // Raw headers as received (lowercased name, value). Owned Strings so the
    // pyclass is Send + Sync (no borrow of the hyper request). Decoded into a
    // Python dict lazily + cached.
    raw_headers: Vec<(String, String)>,
    body: Bytes,
    json_cache: OnceLock<Py<PyAny>>,
    headers_cache: OnceLock<Py<PyAny>>,
    query_cache: OnceLock<Py<PyAny>>,
}

impl Request {
    pub fn new(
        method: String,
        path: String,
        query_string: String,
        raw_headers: Vec<(String, String)>,
        body: Bytes,
    ) -> Self {
        Self {
            method,
            path,
            query_string,
            raw_headers,
            body,
            json_cache: OnceLock::new(),
            headers_cache: OnceLock::new(),
            query_cache: OnceLock::new(),
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

    /// Raw urlencoded query string (e.g. "a=1&b=2"), empty if none. Lazy
    /// parsing into a dict is `query_params`.
    #[getter]
    fn query_string(&self) -> &str {
        &self.query_string
    }

    /// Headers as a {lower-name: value} dict — first value wins (Starlette
    /// parity / header-smuggling defense). Built once, cached.
    #[getter]
    fn headers(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if let Some(v) = self.headers_cache.get() {
            return Ok(v.clone_ref(py));
        }
        let d = PyDict::new(py);
        for (k, v) in &self.raw_headers {
            // setdefault semantics: first occurrence wins.
            if !d.contains(k)? {
                d.set_item(k, v)?;
            }
        }
        let obj = d.into_any().unbind();
        let _ = self.headers_cache.set(obj.clone_ref(py));
        Ok(obj)
    }

    /// Query params parsed into a {key: last-value} dict (urllib.parse_qsl
    /// semantics: last value wins for repeated keys). Lazy + cached.
    #[getter]
    fn query_params(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if let Some(v) = self.query_cache.get() {
            return Ok(v.clone_ref(py));
        }
        let d = PyDict::new(py);
        if !self.query_string.is_empty() {
            // urllib.parse.parse_qsl handles percent-decoding + '+' → space
            // exactly like Starlette's QueryParams. Reuse it (stdlib, no dep).
            let parse = py.import("urllib.parse")?;
            let pairs = parse.call_method1("parse_qsl", (&self.query_string,))?;
            let pairs: Bound<'_, PyList> = pairs.cast_into()?;
            for item in pairs.iter() {
                let k = item.get_item(0)?;
                let v = item.get_item(1)?;
                d.set_item(k, v)?; // last wins (overwrite)
            }
        }
        let obj = d.into_any().unbind();
        let _ = self.query_cache.set(obj.clone_ref(py));
        Ok(obj)
    }

    /// Raw request body bytes.
    #[getter]
    fn body<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.body)
    }

    fn json(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if let Some(v) = self.json_cache.get() {
            return Ok(v.clone_ref(py));
        }
        // Single-pass via pydantic_core if present (Rust core, no second
        // decode); fall back to stdlib json. The bound `from_json` callable is
        // resolved ONCE (perf F4) instead of `import` + lookup per request.
        let body = PyBytes::new(py, &self.body);
        let parsed = if let Some(f) = from_json_fn(py) {
            f.bind(py).call1((body,))?.unbind()
        } else {
            py.import("json")?.call_method1("loads", (body,))?.unbind()
        };
        let _ = self.json_cache.set(parsed.clone_ref(py));
        Ok(parsed)
    }
}

/// The bound `pydantic_core.from_json` callable, resolved once (perf F4 —
/// mirror of `serialize::to_json_fn`). `None` = pydantic_core absent → stdlib.
/// MUST be `PyOnceLock` not `std::OnceLock`: the init `import` releases the GIL,
/// and a std lock held across that deadlocks concurrent GIL-holders (see
/// serialize::to_json_fn for the full rationale).
fn from_json_fn(py: Python<'_>) -> Option<Py<PyAny>> {
    static F: PyOnceLock<Option<Py<PyAny>>> = PyOnceLock::new();
    F.get_or_init(py, || {
        // M7 T4.13: intern! the module name + attr name.
        py.import(pyo3::intern!(py, "pydantic_core"))
            .and_then(|pc| pc.getattr(pyo3::intern!(py, "from_json")))
            .map(|f| f.unbind())
            .ok()
    })
    .as_ref()
    .map(|f| f.clone_ref(py))
}

/// Test-only constructor (gated; the real Request is built by service.rs from
/// the hyper request). Kept for the FFI unit tests that exercise the lazy
/// getters without standing up a server.
#[pyfunction]
pub fn _test_make_request(method: &[u8], path: &[u8], body: &[u8]) -> Request {
    Request::new(
        String::from_utf8_lossy(method).into_owned(),
        String::from_utf8_lossy(path).into_owned(),
        String::new(),
        Vec::new(),
        Bytes::copy_from_slice(body),
    )
}
