#[test]
fn gil_invariant() {
    let t = trybuild::TestCases::new();
    t.pass("tests/ui/gil_ok.rs");
    t.compile_fail("tests/ui/gil_await_fail.rs");
}
