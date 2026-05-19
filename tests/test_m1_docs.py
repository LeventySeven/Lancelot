from lancelot import App
from lancelot.testclient import TestClient


def test_doc_uis_serve_html_pointing_at_openapi():
    with TestClient(App()) as c:
        for path, marker in [("/docs", b"swagger-ui"), ("/redoc", b"redoc"),
                             ("/scalar", b"scalar")]:
            r = c.get(path)
            assert r.status_code == 200, path
            assert r.headers["content-type"].startswith("text/html"), path
            assert b"/openapi.json" in r.content, path
            assert marker in r.content.lower(), path
