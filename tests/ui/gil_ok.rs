// The positive side of the invariant: a SYNC closure passed to with_gil
// compiles AND runs. trybuild executes `pass` fixtures, so initialize the
// embedded interpreter first (cargo-side build links libpython; the maturin
// wheel never hits this path because Python is already running there).
use lancelot::gil::with_gil;

fn main() {
    lancelot::pyo3::Python::initialize();
    let n: usize = with_gil(|_py| 1 + 1);
    assert_eq!(n, 2);
}
