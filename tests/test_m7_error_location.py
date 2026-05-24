"""M7 T4.12 — error messages at the Rust↔Python boundary include
file:line so operator tracebacks name both stacks.

LANCEDB pattern (LANCEDB.md:4023-4067 — `snafu` `#[snafu(implicit)]
location: Location`). Implemented via stdlib `file!()` / `line!()` at
the call sites — 9 sites total, no snafu crate dep (cardinal rule:
pydantic-only runtime).

This test inspects the source for the augmentation; the actual Rust
error path is exercised by `_native.run()` which is only callable from
the production server entry (not from in-process TestClient tests).
The behavior is observable in production tracebacks; this test ensures
the call-site augmentation hasn't been accidentally regressed.
"""
import pathlib

_LIB_RS = pathlib.Path(__file__).resolve().parents[1] / "src" / "lib.rs"
_DISPATCH_RS = pathlib.Path(__file__).resolve().parents[1] / "src" / "dispatch.rs"


def test_t4_12_lib_rs_route_registration_uses_file_line():
    """src/lib.rs route registration errors include `file!():line!()`."""
    src = _LIB_RS.read_text()
    # The augmentation pattern: `file!(), line!(), e, method, path`
    assert "file!()" in src and "line!()" in src, \
        "src/lib.rs missing file!()/line!() augmentation for route registration"
    # Spot-check the specific augmented sites.
    assert "route registration failed at" in src
    assert "const-route prebake failed at" in src
    assert "const-route registration failed at" in src


def test_t4_12_dispatch_rs_panic_includes_file_line():
    """src/dispatch.rs panic-in-join error includes file:line."""
    src = _DISPATCH_RS.read_text()
    assert "file!()" in src and "line!()" in src, \
        "src/dispatch.rs missing file!()/line!() augmentation"
    assert "dispatch task failed at" in src
