from lancelot._native import _ping
from lancelot.app import App, Lancelot, run
from lancelot.concurrency import gather
from lancelot.requests import Request
from lancelot.routing import APIRouter

__all__ = ["App", "Lancelot", "APIRouter", "Request", "TestClient",
           "BackgroundTasks", "background", "WebSocket",
           "WebSocketDisconnect", "StaticFiles", "gather", "run", "_ping"]
__version__ = "0.0.0"


def __getattr__(name):
    # TestClient pulls starlette/httpx (TEST-only deps, not runtime — pyproject
    # declares only pydantic). Lazy per PEP 562 so plain `import lancelot`
    # stays dependency-light (regression guard: M0 DoD #1 imports the wheel
    # with no deps installed). `from lancelot import TestClient` still works.
    if name == "TestClient":
        from lancelot.testclient import TestClient

        return TestClient
    if name == "BackgroundTasks":
        from lancelot.background import BackgroundTasks

        return BackgroundTasks
    if name in ("WebSocket", "WebSocketDisconnect"):
        from lancelot import websockets

        return getattr(websockets, name)
    if name == "StaticFiles":
        from lancelot.staticfiles import StaticFiles

        return StaticFiles
    if name == "llm":
        # M2-P2: lazy so `import lancelot` doesn't pull httpx (runtime deps
        # stay pydantic-only). Use importlib to avoid re-triggering this
        # __getattr__ via `from lancelot import llm`.
        import importlib

        return importlib.import_module("lancelot.llm")
    raise AttributeError(f"module 'lancelot' has no attribute {name!r}")
