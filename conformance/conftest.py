"""THE SWAP (harness-design §4.1): rebind `fastapi` / `fastapi.testclient`
so vendored FastAPI-shaped tests resolve to Lancelot, with zero test edits.
For M1-P1 the corpus is a hand-written seed exercising spine behaviors; the
real pinned 581-file FastAPI tree + reference/candidate differential harness
is M1-P7."""
import sys
import types

import lancelot
from conformance.swap import RealTestClient
from lancelot.params import Depends, Query, Path, Header, Cookie, Body

_fastapi = types.ModuleType("fastapi")
_fastapi.FastAPI = lancelot.App
_fastapi.APIRouter = lancelot.APIRouter
_fastapi.Depends = Depends
_fastapi.Query = Query
_fastapi.Path = Path
_fastapi.Header = Header
_fastapi.Cookie = Cookie
_fastapi.Body = Body
sys.modules["fastapi"] = _fastapi

_tc = types.ModuleType("fastapi.testclient")
_tc.TestClient = RealTestClient
sys.modules["fastapi.testclient"] = _tc
_fastapi.testclient = _tc
