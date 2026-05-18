use pyo3::prelude::*;

/// The ENTIRE Python->Rust handler contract. Captured once at registration;
/// the hot path never introspects a Python signature.
#[pyclass(frozen)]
pub struct FnInfo {
    pub handler: Py<PyAny>,
    #[pyo3(get)]
    pub is_async: bool,
    #[pyo3(get)]
    pub arity: usize,
}

#[pymethods]
impl FnInfo {
    #[new]
    fn new(handler: Py<PyAny>, is_async: bool, arity: usize) -> Self {
        Self {
            handler,
            is_async,
            arity,
        }
    }

    fn call_check(&self, py: Python<'_>) -> &'static str {
        if self.handler.bind(py).is_callable() {
            "callable"
        } else {
            "not-callable"
        }
    }
}
