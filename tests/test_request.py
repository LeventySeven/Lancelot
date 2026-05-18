import lancelot._native as n


def test_lazy_request_materializes_on_access():
    req = n._test_make_request(b"POST", b"/echo", b'{"a":1}')
    assert req.method == "POST"
    assert req.path == "/echo"
    assert req.json() == {"a": 1}
    assert req.json() == {"a": 1}  # cached, second call must not re-parse
