from lancelot._native import _ping
from lancelot.app import App, Lancelot, run
from lancelot.concurrency import gather
from lancelot.requests import Request
from lancelot.routing import APIRouter

# FastAPI users write `from fastapi import FastAPI`; alias so the swap
# `from lancelot import FastAPI` works without any further rewriting.
FastAPI = App

__all__ = [
    # Core surface.
    "App", "FastAPI", "Lancelot", "APIRouter", "Request",
    "TestClient", "gather", "run", "_ping",
    # FastAPI-compat names (re-exported lazily — see __getattr__ below).
    "Depends", "Security",
    "Body", "Query", "Path", "Header", "Cookie", "Form", "File",
    "UploadFile",
    "HTTPException", "RequestValidationError",
    "ProblemDetail", "retry_taxonomy",
    "WebSocketException", "WebSocketDisconnect", "WebSocket",
    "Response", "BackgroundTasks", "background",
    "StaticFiles",
    # M17 P9-cap: safe pagination/sort DEPENDENCIES (DoS-cap + sort-allowlist;
    # NO Page[T] envelope — see lancelot/pagination.py).
    "PageParams", "SortParam",
]
__version__ = "0.1.0"


# FastAPI-compat lazy re-exports. Mapping `name → (module, attr_name)`.
# Kept lazy so plain `import lancelot` doesn't pull starlette/python-multipart/
# httpx (the pydantic-only runtime guard, M0 DoD #1).
_FASTAPI_COMPAT = {
    # DI surface (lancelot.params owns the param-fn dataclasses; same shape as
    # fastapi.params).
    "Depends":         ("lancelot.params", "Depends"),
    "Security":        ("lancelot.params", "Security"),
    "Body":            ("lancelot.params", "Body"),
    "Query":           ("lancelot.params", "Query"),
    "Path":            ("lancelot.params", "Path"),
    "Header":          ("lancelot.params", "Header"),
    "Cookie":          ("lancelot.params", "Cookie"),
    "Form":            ("lancelot.params", "Form"),
    "File":            ("lancelot.params", "File"),
    # Exceptions.
    "HTTPException":           ("lancelot.exceptions", "HTTPException"),
    "RequestValidationError":  ("lancelot.exceptions", "RequestValidationError"),
    "ProblemDetail":           ("lancelot.exceptions", "ProblemDetail"),
    "retry_taxonomy":          ("lancelot.exceptions", "retry_taxonomy"),
    "WebSocketException":      ("lancelot.exceptions", "WebSocketRequestValidationError"),
    # Responses.
    "Response":         ("lancelot.responses", "Response"),
    # File upload helper.
    "UploadFile":       ("lancelot.uploads", "UploadFile"),
}


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
    if name == "canonical":
        # Tier-C.C1: KV-cache-friendly JSON helper. Stdlib-only, cheap to
        # import unconditionally — left lazy for surface symmetry.
        import importlib

        return importlib.import_module("lancelot.canonical")
    if name == "context":
        # Tier-B.B3 surface: request_id ContextVar.
        import importlib

        return importlib.import_module("lancelot.context")
    if name in ("PageParams", "SortParam"):
        # M17 P9-cap: stdlib-only DoS-cap pagination/sort dependencies. Lazy
        # for surface symmetry (the module pulls only stdlib + already-loaded
        # lancelot.exceptions/requests, so it stays runtime-dep-light).
        from lancelot import pagination

        return getattr(pagination, name)
    # FastAPI-compat lazy re-exports (M5 drop-in adoption).
    if name in _FASTAPI_COMPAT:
        mod_name, attr_name = _FASTAPI_COMPAT[name]
        import importlib
        try:
            mod = importlib.import_module(mod_name)
        except ImportError as e:
            raise AttributeError(
                f"lancelot.{name} is provided by an optional dependency "
                f"that isn't installed ({mod_name}: {e}). Install the "
                f"FastAPI-equivalent extras and retry."
            ) from e
        if not hasattr(mod, attr_name):
            raise AttributeError(
                f"lancelot.{name}: {mod_name}.{attr_name} is not exposed by "
                f"this Lancelot version. Check the migration cookbook for "
                f"the supported surface."
            )
        return getattr(mod, attr_name)
    raise AttributeError(f"module 'lancelot' has no attribute {name!r}")
