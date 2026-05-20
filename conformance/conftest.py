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
from lancelot.uploads import UploadFile as _UF  # noqa: E402

_fastapi.UploadFile = _UF
_fastapi.BackgroundTasks = lancelot.BackgroundTasks
_fastapi.Request = lancelot.Request
from lancelot.websockets import WebSocket as _WS, WebSocketDisconnect as _WSD, WebSocketException as _WSE  # noqa: E402

_fastapi.WebSocket = _WS
_fastapi.WebSocketDisconnect = _WSD
_fastapi.WebSocketException = _WSE
import lancelot.responses as _lresp  # noqa: E402

for _rn in ("Response", "JSONResponse", "HTMLResponse", "PlainTextResponse",
            "StreamingResponse"):
    if hasattr(_lresp, _rn):
        setattr(_fastapi, _rn, getattr(_lresp, _rn))
# FileResponse / RedirectResponse: Starlette-provided ASGI responses
# pass-through (tests/users that need them get Starlette's working impl;
# Lancelot's asgi seam recognizes any Response subclass and emits it).
for _srn in ("FileResponse", "RedirectResponse"):
    try:
        _scls = getattr(__import__(
            "starlette.responses", fromlist=[_srn]), _srn)
        setattr(_fastapi, _srn, _scls)
        setattr(_lresp, _srn, _scls)
    except (ImportError, AttributeError):
        pass
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
# fastapi.param_functions: surface alias for Query/Path/Header/Cookie/Body
# (vendored tests do `from fastapi.param_functions import Query`).
import lancelot.params as _lparams  # noqa: E402

_pfm = types.ModuleType("fastapi.param_functions")
for _n in ("Query", "Path", "Header", "Cookie", "Body", "Form", "File",
           "Depends"):
    if hasattr(_lparams, _n):
        setattr(_pfm, _n, getattr(_lparams, _n))
sys.modules["fastapi.param_functions"] = _pfm
_fastapi.param_functions = _pfm
# fastapi.routing surface — vendored docs_src/custom_request_and_route
# subclasses APIRoute to override get_route_handler(). We expose a thin
# shim that subclassing+overriding doesn't break route registration.
import lancelot.routing as _lrouting  # noqa: E402

_routing_mod = types.ModuleType("fastapi.routing")
_routing_mod.APIRoute = _lrouting.Route
_routing_mod.APIRouter = _lrouting.APIRouter
sys.modules["fastapi.routing"] = _routing_mod
_fastapi.routing = _routing_mod
# FastAPI-specific deprecation marker class (vendored tests reference it).
if not hasattr(_lexc, "FastAPIDeprecationWarning"):
    _lexc.FastAPIDeprecationWarning = type(
        "FastAPIDeprecationWarning", (DeprecationWarning,), {})
import lancelot.middleware as _lmw  # noqa: E402

sys.modules["fastapi.middleware"] = _lmw
_fastapi.middleware = _lmw
# fastapi.middleware.cors / .httpsredirect / .trustedhost / .gzip:
# CORS uses Lancelot's native impl; the others are Starlette-provided
# ASGI middlewares (any spec-compliant middleware works with Lancelot).
_mw_cors = types.ModuleType("fastapi.middleware.cors")
_mw_cors.CORSMiddleware = _lmw.CORSMiddleware
sys.modules["fastapi.middleware.cors"] = _mw_cors
for _sname in ("httpsredirect", "trustedhost", "gzip"):
    try:
        _sm = __import__(f"starlette.middleware.{_sname}",
                         fromlist=["*"])
        sys.modules[f"fastapi.middleware.{_sname}"] = _sm
    except ImportError:
        pass
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
_sec.SecurityScopes = _lsec.SecurityScopes  # real impl (di.py magic-inject)
from lancelot.params import Security as _Security  # noqa: E402

_fastapi.Security = _Security
_fastapi.HTTPException = __import__(
    "lancelot.exceptions", fromlist=["HTTPException"]).HTTPException
