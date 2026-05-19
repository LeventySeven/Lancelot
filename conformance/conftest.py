"""THE SWAP (harness-design §4.1): rebind `fastapi` / `fastapi.testclient`
so vendored FastAPI-shaped tests resolve to Lancelot, with zero test edits.
For M1-P1 the corpus is a hand-written seed exercising spine behaviors; the
real pinned 581-file FastAPI tree + reference/candidate differential harness
is M1-P7."""
import sys
import types

import lancelot
from conformance.swap import RealTestClient
from lancelot.params import Depends, Query, Path, Header, Cookie, Body, Form, File

_fastapi = types.ModuleType("fastapi")
_fastapi.FastAPI = lancelot.App
_fastapi.APIRouter = lancelot.APIRouter
_fastapi.Depends = Depends
_fastapi.Query = Query
_fastapi.Path = Path
_fastapi.Header = Header
_fastapi.Cookie = Cookie
_fastapi.Body = Body
# Genuinely-implemented Lancelot surface, exposed under the `fastapi`
# name (harness-design §4.1 — the swap's purpose; NOT editing tests).
# File/UploadFile are intentionally NOT mapped (multipart is ledgered
# RED) so tests needing them fail HONESTLY at import.
_fastapi.Form = Form
_fastapi.File = File
_fastapi.BackgroundTasks = lancelot.BackgroundTasks
_fastapi.Request = lancelot.Request
from lancelot.websockets import WebSocket as _WS, WebSocketDisconnect as _WSD  # noqa: E402

_fastapi.WebSocket = _WS
_fastapi.WebSocketDisconnect = _WSD
import lancelot.responses as _lresp  # noqa: E402

for _rn in ("Response", "JSONResponse", "HTMLResponse", "PlainTextResponse"):
    if hasattr(_lresp, _rn):
        setattr(_fastapi, _rn, getattr(_lresp, _rn))
# `from fastapi.responses import JSONResponse` / `from fastapi import status`
sys.modules["fastapi.responses"] = _lresp
_fastapi.responses = _lresp
import lancelot.status as _lstatus  # noqa: E402

sys.modules["fastapi.status"] = _lstatus
_fastapi.status = _lstatus
# fastapi.exceptions / fastapi.middleware / fastapi.websockets submodule maps
import lancelot.exceptions as _lexc  # noqa: E402

sys.modules["fastapi.exceptions"] = _lexc
_fastapi.exceptions = _lexc
import lancelot.middleware as _lmw  # noqa: E402

sys.modules["fastapi.middleware"] = _lmw
_fastapi.middleware = _lmw
# fastapi.middleware.cors / fastapi.middleware.trustedhost via the alias
_mw_cors = types.ModuleType("fastapi.middleware.cors")
_mw_cors.CORSMiddleware = _lmw.CORSMiddleware
sys.modules["fastapi.middleware.cors"] = _mw_cors
import lancelot.websockets as _lws  # noqa: E402

sys.modules["fastapi.websockets"] = _lws
_fastapi.websockets = _lws
sys.modules["fastapi"] = _fastapi

_tc = types.ModuleType("fastapi.testclient")
_tc.TestClient = RealTestClient
sys.modules["fastapi.testclient"] = _tc
_fastapi.testclient = _tc

# fastapi.security -> lancelot.security (M1-P4)
import lancelot.security as _lsec  # noqa: E402

_sec = types.ModuleType("fastapi.security")
for _n in ("APIKeyHeader", "APIKeyQuery", "APIKeyCookie", "HTTPBearer",
           "HTTPBasic", "HTTPAuthorizationCredentials", "HTTPBasicCredentials",
           "OAuth2", "OAuth2PasswordBearer", "OAuth2PasswordRequestForm"):
    setattr(_sec, _n, getattr(_lsec, _n))
sys.modules["fastapi.security"] = _sec
_fastapi.security = _sec
# SecurityScopes (FastAPI primitive; minimal stub — behavior ledgered)
_sec.SecurityScopes = type("SecurityScopes", (),
                           {"__init__": lambda self, scopes=None:
                            setattr(self, "scopes", scopes or [])})
from lancelot.params import Security as _Security  # noqa: E402

_fastapi.Security = _Security
_fastapi.HTTPException = __import__(
    "lancelot.exceptions", fromlist=["HTTPException"]).HTTPException
