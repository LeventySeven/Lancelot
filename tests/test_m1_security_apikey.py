import asyncio

import pytest

from lancelot.security import APIKeyHeader, APIKeyQuery, APIKeyCookie
from lancelot.exceptions import HTTPException
from lancelot.requests import Request


def _req(headers=None, query=None, cookies=None):
    return Request("GET", "/", query or {}, headers or {}, cookies or {},
                   {}, b"", None)


def _run(co):
    return asyncio.new_event_loop().run_until_complete(co)


def test_apikey_header_extract_and_401():
    h = APIKeyHeader(name="key")
    assert h.model == {"type": "apiKey", "in": "header", "name": "key"}
    assert _run(h(_req(headers={"key": "secret"}))) == "secret"
    with pytest.raises(HTTPException) as e:
        _run(h(_req(headers={})))
    assert e.value.status_code == 401
    assert e.value.detail == "Not authenticated"
    assert e.value.headers == {"WWW-Authenticate": "APIKey"}  # MUST-2
    # MUST-4: present-but-empty == missing
    with pytest.raises(HTTPException):
        _run(h(_req(headers={"key": ""})))
    # auto_error=False -> None, no raise
    h2 = APIKeyHeader(name="key", auto_error=False)
    assert _run(h2(_req(headers={}))) is None


def test_apikey_query_and_cookie():
    q = APIKeyQuery(name="k")
    assert q.model == {"type": "apiKey", "in": "query", "name": "k"}
    assert _run(q(_req(query={"k": "v"}))) == "v"
    ck = APIKeyCookie(name="sid", description="An API Key Cookie")
    assert ck.model == {"type": "apiKey", "in": "cookie", "name": "sid",
                        "description": "An API Key Cookie"}
    assert _run(ck(_req(cookies={"sid": "abc"}))) == "abc"
