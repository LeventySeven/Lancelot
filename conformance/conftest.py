"""THE SWAP (harness-design §4.1): rebind `fastapi` / `fastapi.testclient`
so vendored FastAPI-shaped tests resolve to Lancelot, with zero test edits.
For M1-P1 the corpus is a hand-written seed exercising spine behaviors; the
real pinned 581-file FastAPI tree + reference/candidate differential harness
is M1-P7."""
import sys
import types

import lancelot
from conformance.swap import RealTestClient

_fastapi = types.ModuleType("fastapi")
_fastapi.FastAPI = lancelot.App
_fastapi.APIRouter = lancelot.APIRouter
sys.modules["fastapi"] = _fastapi

_tc = types.ModuleType("fastapi.testclient")
_tc.TestClient = RealTestClient
sys.modules["fastapi.testclient"] = _tc
_fastapi.testclient = _tc
