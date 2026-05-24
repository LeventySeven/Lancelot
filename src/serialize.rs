use bytes::Bytes;
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

/// The bound `orjson.dumps` callable, resolved ONCE (M6-T1.2b). orjson is a
/// SOFT dep — not in pyproject's runtime deps (cardinal rule: pydantic-only) —
/// but when installed it's ~2.2× faster than pydantic_core.to_json on
/// dict-heavy payloads per the M5 lab body (Robyn 8.1k vs us 5.0k @ 70KB).
/// `None` = orjson absent → use pydantic-core via `to_json_fn`. Same
/// `PyOnceLock` discipline as `to_json_fn` for GIL/deadlock safety.
fn orjson_dumps_fn(py: Python<'_>) -> Option<Py<PyAny>> {
    static F: PyOnceLock<Option<Py<PyAny>>> = PyOnceLock::new();
    F.get_or_init(py, || {
        py.import("orjson")
            .and_then(|oj| oj.getattr("dumps"))
            .map(|f| f.unbind())
            .ok()
    })
    .as_ref()
    .map(|f| f.clone_ref(py))
}

/// One-pass typed serialization: try orjson first (M6-T1.2b — ~2.2× speedup
/// on dict-heavy payloads when installed), fall through to pydantic_core.to_json
/// when orjson can't serialize a value (TypeError; e.g. BaseModel without
/// OPT_SERIALIZE_PYDANTIC, Decimal, etc.). Last resort: stdlib json.
pub fn to_json_bytes(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Bytes> {
    // Fast path 1: orjson.dumps (when installed). On TypeError, fall through
    // to pydantic-core which handles the richer set of types natively.
    if let Some(f) = orjson_dumps_fn(py) {
        match f.bind(py).call1((value,)) {
            Ok(out) => {
                return Ok(Bytes::copy_from_slice(
                    out.cast::<PyBytes>()?.as_bytes(),
                ));
            }
            Err(e) if e.is_instance_of::<pyo3::exceptions::PyTypeError>(py) => {
                e.restore(py); // Clear the error before falling through.
                pyo3::PyErr::take(py);
            }
            Err(e) => return Err(e),
        }
    }
    // Fast path 2: pydantic_core.to_json (Rust core, bytes out, no intermediate
    // dict, no second json.dumps). Handles BaseModel/UUID/datetime/Decimal.
    if let Some(f) = to_json_fn(py) {
        let out = f.bind(py).call1((value,))?; // -> bytes (cast, not deprecated downcast)
                                               // Copy straight into `Bytes` — kills the second
                                               // Vec→Bytes alloc the response/stream path did (perf:
                                               // one fewer alloc + memcpy per response/chunk).
        return Ok(Bytes::copy_from_slice(out.cast::<PyBytes>()?.as_bytes()));
    }
    let json = py.import("json")?;
    let s: String = json.call_method1("dumps", (value,))?.extract()?;
    Ok(Bytes::from(s.into_bytes()))
}

#[pyfunction]
pub fn _to_json<'py>(py: Python<'py>, value: Bound<'py, PyAny>) -> PyResult<Py<PyBytes>> {
    let bytes = to_json_bytes(py, &value)?; // Bytes derefs to &[u8] for PyBytes::new
    Ok(PyBytes::new(py, &bytes).unbind())
}
