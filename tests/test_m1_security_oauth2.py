import asyncio

import pytest

from lancelot.security import OAuth2PasswordBearer, OAuth2, OAuth2PasswordRequestForm
from lancelot.exceptions import HTTPException
from lancelot.requests import Request


def _req(auth=None):
    return Request("GET", "/", {}, ({"authorization": auth} if auth else {}),
                   {}, {}, b"", None)


def _run(co):
    return asyncio.new_event_loop().run_until_complete(co)


def test_oauth2_password_bearer():
    o = OAuth2PasswordBearer(tokenUrl="token")
    assert o.model == {"type": "oauth2",
                       "flows": {"password": {"scopes": {}, "tokenUrl": "token"}}}
    assert _run(o(_req("Bearer abc"))) == "abc"  # token only (param)
    with pytest.raises(HTTPException) as e:
        _run(o(_req("Basic abc")))  # scheme != bearer
    assert e.value.status_code == 401
    assert e.value.headers == {"WWW-Authenticate": "Bearer"}
    assert _run(OAuth2PasswordBearer(tokenUrl="t", auto_error=False)
                (_req(None))) is None


def test_oauth2_base_returns_raw_header():
    b = OAuth2()
    assert _run(b(_req("Other footoken"))) == "Other footoken"  # MUST-9 raw


def test_oauth2_scopes_in_model():
    o = OAuth2PasswordBearer(tokenUrl="token", scopes={"read": "Read"})
    assert o.model["flows"]["password"] == {"scopes": {"read": "Read"},
                                            "tokenUrl": "token"}


def test_password_request_form_surface():
    f = OAuth2PasswordRequestForm(username="u", password="p", scope="a b")
    assert f.username == "u" and f.password == "p" and f.scopes == ["a", "b"]
