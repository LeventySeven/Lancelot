import dataclasses
import re
import typing
from dataclasses import dataclass
from typing import Any, Callable


def _is_body_annotation(ann) -> bool:
    """FastAPI body rule (deep/01 §3.1): a pydantic model, a dataclass, or a
    container (list/set/tuple/dict) whose element is a model → request body
    (not a scalar query param). Plain scalars/scalar-containers stay query."""
    if isinstance(ann, type) and hasattr(ann, "model_validate_json"):
        return True
    if dataclasses.is_dataclass(ann):
        return True
    origin = typing.get_origin(ann)
    if origin in (list, set, frozenset, tuple, dict):
        return any(
            isinstance(a, type) and hasattr(a, "model_validate_json")
            for a in typing.get_args(ann)
        )
    return False


@dataclass
class Route:
    method: str
    path: str
    handler: Callable[..., Any]
    name: str = ""
    include_in_schema: bool = True  # FastAPI: doc/openapi routes are False
    is_ws: bool = False  # WebSocket route (matched by scope type, not method)
    status_code: int | None = None  # FastAPI default response status
    # FastAPI route-decorator kwargs accepted-and-stored; behavior ledgered:
    response_model: Any = None
    response_model_include: Any = None
    response_model_exclude: Any = None
    response_model_by_alias: bool = True
    response_model_exclude_unset: bool = False
    response_model_exclude_defaults: bool = False
    response_model_exclude_none: bool = False
    response_class: Any = None
    responses: dict | None = None
    operation_id: str | None = None
    summary: str | None = None
    description: str | None = None
    tags: list | None = None
    dependencies: list | None = None
    deprecated: bool | None = None

    def __post_init__(self):
        self.method = self.method.upper()
        if not self.name:
            self.name = getattr(self.handler, "__name__", "route")
        # /items/{item_id} -> ^/items/(?P<item_id>[^/]+)$
        # B1: literal segments MUST be regex-escaped (a raw '.' or '(' in the
        # dev's path otherwise matches any char / raises re.error). Split on
        # the {param} delimiters; escape literals, only params become groups.
        rx = ""
        for part in re.split(r"({[^}/]+})", self.path):
            m = re.fullmatch(r"{([^}/]+)}", part)
            rx += f"(?P<{m.group(1)}>[^/]+)" if m else re.escape(part)
        self._rx = re.compile("^" + rx + "$")

        # Registration-time: compile the DI/validation plan ONCE (U15 — the
        # hot path never re-introspects). Detect a pydantic-model body param
        # and lift it out of scalar validation (validated single-pass at the
        # ASGI seam). list[Model]/dataclass bodies are long-tail → P7 ratchet.
        from lancelot.di import compile_dependant
        from lancelot.validation import BodyValidator, FormBodyValidator

        self.plan = compile_dependant(
            self.handler, path_params=set(self._rx.groupindex)
        )
        # FastAPI rule (test_invalid_path_param): path params must be
        # primitive-coercible — NOT collections/dicts/BaseModels.
        for fi in self.plan.fields:
            if getattr(fi.marker, "in_", None) != "path":
                continue
            ann = fi.annotation
            origin = typing.get_origin(ann) or ann
            if origin in (list, tuple, set, frozenset, dict) \
                    or (isinstance(ann, type)
                        and hasattr(ann, "model_validate")):
                raise AssertionError(
                    f"Path param '{fi.name}' must be a primitive type; "
                    f"got {ann!r}")
        self.body = None
        self.body_kind = "json"  # "json" | "form"  (multipart ledgered)
        for fi in list(self.plan.fields):
            if not _is_body_annotation(fi.annotation):
                continue
            mk = getattr(fi.marker, "in_", None)
            # Query/Header/Cookie BUCKET model only when EXPLICIT (the user
            # wrote `= Query()`); a bare `item: Item` (implicit-Query
            # default) is FastAPI's body-by-inference rule — still lifted.
            if fi.explicit_marker and mk in ("query", "header", "cookie"):
                continue
            # `Annotated[Model, Form()]` → urlencoded form-body, not JSON.
            if mk == "form":
                self.body = (fi.name, FormBodyValidator(fi.annotation))
                self.body_kind = "form"
            else:
                self.body = (fi.name, BodyValidator(fi.annotation))
            self.plan.fields = [f for f in self.plan.fields
                                if f.name != fi.name]
            self.plan.validators.pop(fi.name, None)

    def match(self, path: str):
        m = self._rx.match(path)
        return m.groupdict() if m else None


class APIRouter:
    def __init__(self, prefix: str = "", tags: list | None = None):
        assert prefix == "" or prefix.startswith("/"), "prefix must start with /"
        self.prefix = prefix.rstrip("/")
        self.tags = tags or []
        self.routes: list[Route] = []

    def _add(self, method: str, path: str, **kwargs):
        # Filter to Route's known kwargs; unknown kwargs are accepted at
        # the App._add layer and dropped here (FastAPI surface that
        # Lancelot doesn't behave on yet — collection-unblock only).
        import dataclasses as _dc

        known = {f.name for f in _dc.fields(Route)}
        kw = {k: v for k, v in kwargs.items() if k in known}

        def deco(fn):
            self.routes.append(Route(method, self.prefix + path, fn, **kw))
            return fn

        return deco

    def get(self, p, **kw): return self._add("GET", p, **kw)
    def post(self, p, **kw): return self._add("POST", p, **kw)
    def put(self, p, **kw): return self._add("PUT", p, **kw)
    def delete(self, p, **kw): return self._add("DELETE", p, **kw)
    def patch(self, p, **kw): return self._add("PATCH", p, **kw)
    def head(self, p, **kw): return self._add("HEAD", p, **kw)
    def options(self, p, **kw): return self._add("OPTIONS", p, **kw)

    def websocket(self, path: str):
        def deco(fn):
            self.routes.append(
                Route("GET", self.prefix + path, fn, is_ws=True))
            return fn

        return deco

    def include_router(self, router: "APIRouter", prefix: str = ""):
        add_prefix = (self.prefix + prefix).rstrip("/")
        for r in router.routes:
            self.routes.append(Route(r.method, add_prefix + r.path, r.handler, r.name))


class Router:
    """App-wide matcher (pure-Python; the ASGI path uses this. The M0 Rust
    matchit router still serves the hyper path — unifying both onto an FFI
    `process_one` is a documented M1+ carry-forward, not M1-P1 scope)."""

    def __init__(self):
        self.routes: list[Route] = []

    def add(self, route: Route):
        self.routes.append(route)

    def match(self, method: str, path: str):
        method = method.upper()
        path_exists = False
        for r in self.routes:
            if r.is_ws:  # WebSocket routes are matched via match_ws only
                continue
            params = r.match(path)
            if params is None:
                continue
            path_exists = True
            if r.method == method:
                return r, params
        return (None, 405) if path_exists else (None, 404)

    def match_ws(self, path: str):
        for r in self.routes:
            if not r.is_ws:
                continue
            params = r.match(path)
            if params is not None:
                return r, params
        return None, 404
