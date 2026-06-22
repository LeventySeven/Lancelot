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
        // M7 T4.13: intern the hot-path module + attr names.
        py.import(pyo3::intern!(py, "pydantic_core"))
            .and_then(|pc| pc.getattr(pyo3::intern!(py, "to_json")))
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
        // M7 T4.13: intern the hot-path module + attr names.
        py.import(pyo3::intern!(py, "orjson"))
            .and_then(|oj| oj.getattr(pyo3::intern!(py, "dumps")))
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
///
/// M7 T4.7 audit (DataFusion `consume_budget` pattern): the BULK work in
/// this function is the single `orjson.dumps()` / `pydantic_core.to_json()`
/// C-extension call, which releases the GIL internally. There is NO
/// Rust-side iteration loop over MB-size payloads — Lancelot delegates
/// bulk serialization to those native libraries which manage their own
/// cooperation. The single `Bytes::copy_from_slice` at the end is a C-level
/// memcpy with no yield-point need (it executes in <5ms even for 10MB).
/// Conclusion: no `tokio::task::coop::consume_budget` call needed here.
/// If a future op introduces a Rust-side loop over MB-size data (e.g. a
/// canonical-JSON re-encoder that walks the dict tree in Rust), THAT op
/// should call `consume_budget` once per ~1k iterations.
pub fn to_json_bytes(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Bytes> {
    // Fast path 1: orjson.dumps (when installed). On TypeError, fall through
    // to pydantic-core which handles the richer set of types natively.
    if let Some(f) = orjson_dumps_fn(py) {
        match f.bind(py).call1((value,)) {
            Ok(out) => {
                return Ok(Bytes::copy_from_slice(out.cast::<PyBytes>()?.as_bytes()));
            }
            Err(e) if e.is_instance_of::<pyo3::exceptions::PyTypeError>(py) => {
                // orjson raised TypeError on a non-serializable input — fall
                // through to the pydantic-core path. pyo3 already detached the
                // error into `e` (CPython indicator is clear), so dropping it is
                // enough; the old restore()+take() pair was a no-op round-trip.
                drop(e);
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

/// The `lancelot.responses.Response` class, resolved ONCE (same perf/GIL
/// discipline as `to_json_fn`). Used to cheaply gate the native value path: an
/// `is_instance` check against this class tells us whether the handler returned
/// a Response object (carry its status/content-type/headers) vs a plain value
/// (the hot JSON path). `None` = the class couldn't be imported (should never
/// happen — lancelot.responses is a sibling module — but fail OPEN to the plain
/// JSON path rather than 500). MUST use `PyOnceLock`, not `std::sync::OnceLock`:
/// the init `import`s a Python module which can release the GIL mid-init (see
/// the `to_json_fn` doc for the deadlock this avoids).
fn response_cls(py: Python<'_>) -> Option<Py<PyAny>> {
    static C: PyOnceLock<Option<Py<PyAny>>> = PyOnceLock::new();
    C.get_or_init(py, || {
        py.import(pyo3::intern!(py, "lancelot.responses"))
            .and_then(|m| m.getattr(pyo3::intern!(py, "Response")))
            .map(|c| c.unbind())
            .ok()
    })
    .as_ref()
    .map(|c| c.clone_ref(py))
}

/// What the native server emits for a value-returning handler: the body bytes
/// PLUS the status / content-type / extra headers a returned `Response` carries.
/// For a plain dict/str/model return it's just `{200, None, [], json_bytes}` —
/// the existing always-200-application/json contract, unchanged.
/// The tuple shape `Response._render_native()` returns:
/// `(status, content_type, headers, body)`. Aliased to keep the `extract()`
/// call site readable (clippy::type_complexity).
type RenderTuple = (u16, Option<String>, Vec<(String, String)>, Vec<u8>);

pub struct Rendered {
    pub status: u16,
    /// `None` → the server defaults to `application/json` (the plain-value
    /// contract). `Some(ct)` → a Response's media_type (incl. charset).
    pub content_type: Option<String>,
    /// Extra response headers (lowercased keys, one entry per value — set-cookie
    /// lists already expanded by the Python helper). Empty for plain values.
    pub headers: Vec<(String, String)>,
    pub body: Bytes,
}

/// Render a handler's return value into a `Rendered` (body + status + headers).
///
/// Hot path (the common case — dict / str / model): a single cheap
/// `is_instance` check that says "not a Response", then the EXISTING
/// orjson/pydantic-core `to_json_bytes` fast path, wrapped as a 200 with no
/// content-type override and no extra headers. Plain values do NOT touch any
/// Python helper — byte-identical to the old `to_json_bytes` path.
///
/// Response path (probes / docs / openapi / a user `return JSONResponse(...)`):
/// call the Python `Response._render_native()` helper (keeps Response internals
/// in Python) and unpack its `(status, content_type, headers, body)` tuple.
pub fn render_result(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Rendered> {
    // Cheap gate: is this a lancelot Response? `is_instance` is a C-level
    // isinstance — fast. If the class import failed (response_cls -> None), fall
    // through to the plain JSON path (fail-open, never 500 on a probe).
    if let Some(cls) = response_cls(py) {
        if value.is_instance(cls.bind(py))? {
            // Delegate the status/header/body construction to Python (mirrors
            // the ASGI seam's _emit_response_object so native == ASGI).
            let rendered = value.call_method0(pyo3::intern!(py, "_render_native"))?;
            let (status, content_type, headers, body): RenderTuple = rendered.extract()?;
            return Ok(Rendered {
                status,
                content_type,
                headers,
                body: Bytes::from(body),
            });
        }
    }
    // Plain value (hot path): the unchanged JSON fast path, status 200, no
    // content-type override, no extra headers.
    let body = to_json_bytes(py, value)?;
    Ok(Rendered {
        status: 200,
        content_type: None,
        headers: Vec::new(),
        body,
    })
}
