// This MUST NOT compile: `.await` inside the sync closure passed to with_gil.
// (Uses std::future::ready, not the `futures` crate, so the compile error is
// specifically the GIL-invariant violation — `.await` in a non-async closure —
// and not an unrelated unresolved-import error.)
use lancelot::gil::with_gil;

async fn bad() {
    with_gil(|_py| {
        std::future::ready(()).await; // error: `await` only allowed in async fn/block
    });
}

fn main() {}
