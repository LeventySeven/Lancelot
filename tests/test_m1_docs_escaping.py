"""A2: doc-UI builders must not let title/openapi_url BREAK OUT of their
HTML/JS context (inert escaped substrings are fine — only breakout is the
vuln). Defense-in-depth: these are public functions."""
import json

from lancelot.docs import swagger_ui_html, redoc_html, scalar_html


def test_title_cannot_break_out_of_html():
    for fn in (swagger_ui_html, redoc_html, scalar_html):
        h = fn("/openapi.json", "</title><script>alert(1)</script>")
        assert "<script>alert(1)</script>" not in h  # raw tag must not form
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in h  # escaped instead


def test_openapi_url_cannot_break_out_of_js_string():
    payload = '","x":alert(1),"y":"'
    h = swagger_ui_html(payload, "T")
    # the url must be exactly the JSON-encoded literal; the raw
    # quote-breakout sequence must NOT appear unescaped.
    assert json.dumps(payload) in h
    assert '","x":alert(1)' not in h  # unescaped breakout absent


def test_openapi_url_cannot_break_out_of_html_attr():
    for fn in (redoc_html, scalar_html):
        r = fn('"><img src=x onerror=alert(1)>', "T")
        assert '"><img' not in r            # raw attr/tag breakout absent
        assert "&lt;img" in r               # escaped instead
