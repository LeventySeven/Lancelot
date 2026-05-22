use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyBytes;

/// The bound `pydantic_core.to_json` callable, resolved ONCE (perf F4). The old
/// code ran `py.import("pydantic_core")` + a method lookup on EVERY serialize —
/// cheap (a sys.modules dict hit) but not free at high RPS. Memoize the callable
/// itself: `Some(f)` = use it; `None` = pydantic_core unavailable → stdlib json.
///
/// MUST use pyo3's `PyOnceLock`, NOT `std::sync::OnceLock`: the init `import`s
/// pydantic_core, and the FIRST import RELEASES the GIL while running module
/// code. `OnceLock::get_or_init` holds an internal blocking lock across that —
/// so a second tokio worker that grabs the GIL meanwhile and calls this would
/// block on the lock WHILE HOLDING THE GIL, and the initializing thread can't
/// re-acquire the GIL to finish → deadlock (observed: concurrent requests
/// timed out). `PyOnceLock` is GIL-aware (releases the GIL while waiting on a
/// concurrent init) so it neither deadlocks nor races.
fn to_json_fn(py: Python<'_>) -> Option<Py<PyAny>> {
    static F: PyOnceLock<Option<Py<PyAny>>> = PyOnceLock::new();
    F.get_or_init(py, || {
        py.import("pydantic_core")
            .and_then(|pc| pc.getattr("to_json"))
            .map(|f| f.unbind())
            .ok()
    })
    .as_ref()
    .map(|f| f.clone_ref(py))
}

/// One-pass typed serialization: delegate to pydantic_core.to_json (Rust core,
/// bytes out, no intermediate dict, no second json.dumps). Falls back to stdlib
/// json only if pydantic_core is unavailable.
pub fn to_json_bytes(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    if let Some(f) = to_json_fn(py) {
        let out = f.bind(py).call1((value,))?; // -> bytes
                                               // pyo3 0.27.2 deprecates downcast in favour of Bound::cast.
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
