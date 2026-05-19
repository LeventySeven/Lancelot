import asyncio
import base64

import pytest

from lancelot.security import (HTTPBearer, HTTPBasic, HTTPAuthorizationCredentials,
                               HTTPBasicCredentials)
from lancelot.exceptions import HTTPException
from lancelot.requests import Request


def _req(auth=None):
    h = {"authorization": auth} if auth else {}
    return Request("GET", "/", {}, h, {}, {}, b"", None)


def _run(co):
    return asyncio.new_event_loop().run_until_complete(co)


def test_http_bearer_two_step():
    b = HTTPBearer()
    assert b.model == {"type": "http", "scheme": "bearer"}
    c = _run(b(_req("Bearer tok123")))
    assert isinstance(c, HTTPAuthorizationCredentials)
    assert c.scheme == "Bearer" and c.credentials == "tok123"
    with pytest.raises(HTTPException) as e:  # wrong scheme -> 401 (MUST-7)
        _run(b(_req("Basic xxx")))
    assert e.value.status_code == 401
    assert e.value.headers == {"WWW-Authenticate": "Bearer"}
    with pytest.raises(HTTPException):  # missing
        _run(b(_req(None)))
    assert _run(HTTPBearer(auto_error=False)(_req("Basic x"))) is None


def test_http_basic_realm_and_split():
    bb = HTTPBasic(realm="simple")
    raw = base64.b64encode(b"u:p:extra").decode()
    cr = _run(bb(_req(f"Basic {raw}")))
    assert isinstance(cr, HTTPBasicCredentials)
    assert cr.username == "u" and cr.password == "p:extra"  # MUST-6 first ':'
    with pytest.raises(HTTPException) as e:
        _run(bb(_req(None)))
    assert e.value.headers == {"WWW-Authenticate": 'Basic realm="simple"'}  # MUST-5
    # MUST-12: bad b64 raises 401 even with auto_error=False
    with pytest.raises(HTTPException):
        _run(HTTPBasic(auto_error=False)(_req("Basic !!!notb64")))
    assert _run(HTTPBasic(auto_error=False)(_req(None))) is None
