from lancelot._native import _ping
from lancelot.app import App, Lancelot, run
from lancelot.requests import Request
from lancelot.routing import APIRouter

__all__ = ["App", "Lancelot", "APIRouter", "Request", "TestClient", "run",
           "_ping"]
__version__ = "0.0.0"


def __getattr__(name):
    # TestClient pulls starlette/httpx (TEST-only deps, not runtime — pyproject
    # declares only pydantic). Lazy per PEP 562 so plain `import lancelot`
    # stays dependency-light (regression guard: M0 DoD #1 imports the wheel
    # with no deps installed). `from lancelot import TestClient` still works.
    if name == "TestClient":
        from lancelot.testclient import TestClient

        return TestClient
    raise AttributeError(f"module 'lancelot' has no attribute {name!r}")
