import dataclasses
import re
import typing
from dataclasses import dataclass
from typing import Any, Callable


def _is_body_annotation(ann) -> bool:
    """FastAPI body rule (deep/01 §3.1): a pydantic model, a dataclass, a
    bare `dict` annotation, or a container (list/set/tuple/dict) whose
    element is a model → request body (not a scalar query param). Plain
    scalars/scalar-containers stay query."""
    if isinstance(ann, type) and hasattr(ann, "model_validate_json"):
        return True
    if dataclasses.is_dataclass(ann):
        return True
    # Bare `dict` (any JSON object body — FastAPI accepts JSON for this).
    if ann is dict:
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
    is_stream: bool = False  # @app.stream: yields lancelot.stream.Event
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
        # /items/{id:int} -> ^/items/(?P<id>\d+)$  (Starlette URL convertors)
        # B1: literal segments MUST be regex-escaped (a raw '.' or '(' in the
        # dev's path otherwise matches any char / raises re.error). Split on
        # the {param} delimiters; escape literals, only params become groups.
        _CONV = {
            "int": r"\d+",
            "float": r"-?\d+(\.\d+)?",
            "str": r"[^/]+",
            "uuid": r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            "path": r".+",
        }
        rx = ""
        for part in re.split(r"({[^}]+})", self.path):
            m = re.fullmatch(r"{([^}:]+)(?::([^}]+))?}", part)
            if m:
                name, conv = m.group(1), m.group(2)
                pat = _CONV.get(conv, "[^/]+")
                rx += f"(?P<{name}>{pat})"
            else:
                rx += re.escape(part)
        self._rx = re.compile("^" + rx + "$")

        # Validate response_model at REGISTRATION time (FastAPI parity):
        # a non-Pydantic-compatible type raises FastAPIError so the dev's
        # mistake surfaces immediately, not at first request.
        if self.response_model is not None:
            from lancelot.exceptions import FastAPIError

            try:
                from pydantic import TypeAdapter as _TA

                _TA(self.response_model)
            except Exception as _e:
                raise FastAPIError(
                    f"Invalid response_model {self.response_model!r}"
                ) from _e
        # Same check for responses[STATUS]["model"] entries.
        if self.responses:
            from lancelot.exceptions import FastAPIError
            from pydantic import TypeAdapter as _TA

            for _k, _spec in self.responses.items():
                _m = (_spec or {}).get("model") if isinstance(
                    _spec, dict) else None
                if _m is None:
                    continue
                try:
                    _TA(_m)
                except Exception as _e:
                    raise FastAPIError(
                        f"Invalid responses[{_k!r}].model {_m!r}"
                    ) from _e
        # Registration-time: compile the DI/validation plan ONCE (U15 — the
        # hot path never re-introspects). Detect a pydantic-model body param
        # and lift it out of scalar validation (validated single-pass at the
        # ASGI seam). list[Model]/dataclass bodies are long-tail → P7 ratchet.
        from lancelot.di import compile_dependant, SubDep
        from lancelot.validation import BodyValidator, FormBodyValidator

        self.plan = compile_dependant(
            self.handler, path_params=set(self._rx.groupindex)
        )
        # Route-level dependencies (dependencies=[Depends(...)]) are
        # invoked with no kwargs-binding (their return value is discarded —
        # FastAPI semantics) but their validation/auth failures DO
        # propagate as 422/401/etc. Append them to plan.dependencies with
        # an unused param_name slot so solve_async runs them.
        for i, dep in enumerate(self.dependencies or []):
            from lancelot.params import Depends as _Dep

            if not isinstance(dep, _Dep):
                continue
            sub_plan = compile_dependant(dep.dependency)
            self.plan.dependencies.append(SubDep(
                dep.dependency, sub_plan, dep.use_cache,
                f"__route_dep_{i}__", discard=True))
        # FastAPI rule (test_invalid_path_param + test_invalid_sequence_param):
        # path params must be primitive-coercible (not collection/dict/Model);
        # query/header/cookie sequences must NOT have Model/dict elements.
        for fi in self.plan.fields:
            in_ = getattr(fi.marker, "in_", None)
            ann = fi.annotation
            origin = typing.get_origin(ann) or ann
            args = typing.get_args(ann)
            if in_ == "path":
                if origin in (list, tuple, set, frozenset, dict) \
                        or (isinstance(ann, type)
                            and hasattr(ann, "model_validate")):
                    raise AssertionError(
                        f"Path param '{fi.name}' must be a primitive type; "
                        f"got {ann!r}")
            elif in_ in ("query", "header", "cookie") and fi.explicit_marker:
                # Only reject when the user EXPLICITLY wrote Query()/etc. —
                # a bare `items: list[Item]` still infers JSON body.
                if origin in (list, tuple, set, frozenset) and args:
                    inner = args[0]
                    if (isinstance(inner, type)
                            and hasattr(inner, "model_validate")) \
                            or typing.get_origin(inner) is dict:
                        raise AssertionError(
                            f"{in_.title()} parameter '{fi.name}' must be "
                            "one of the supported types")
                elif origin is dict:
                    raise AssertionError(
                        f"{in_.title()} parameter '{fi.name}' must be "
                        "one of the supported types")
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
    def __init__(self, prefix: str = "", tags: list | None = None, **kwargs):
        # Accept FastAPI's documented APIRouter surface (dependencies,
        # default_response_class, strict_content_type, prefix_options,
        # responses, callbacks, deprecated, include_in_schema, ...). The
        # ones we genuinely apply pull from self._router_kwargs at
        # include_router time; the rest are surface-only (collection-
        # unblock). NEVER edit the test — accept and ledger.
        assert prefix == "" or prefix.startswith("/"), "prefix must start with /"
        self.prefix = prefix.rstrip("/")
        self.tags = tags or list(kwargs.pop("tags", None) or [])
        self.dependencies = list(kwargs.pop("dependencies", []) or [])
        self.default_response_class = kwargs.pop(
            "default_response_class", None)
        self.deprecated = kwargs.pop("deprecated", None)
        # Keep the rest as opaque (Lancelot doesn't behave on them yet).
        self._router_kwargs = kwargs
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

    def include_router(self, router: "APIRouter", prefix: str = "",
                       **kwargs):
        # FastAPI: include_router accepts dependencies, default_response_class,
        # responses, tags, deprecated, include_in_schema, callbacks. The ones
        # Lancelot acts on: dependencies (prepended), default_response_class
        # (applied to child routes without their own response_class), tags
        # (extended). The rest are surface-only collection-unblocks.
        add_prefix = (self.prefix + prefix).rstrip("/")
        extra_deps = list(kwargs.pop("dependencies", None) or [])
        default_rc = kwargs.pop("default_response_class",
                                getattr(self, "default_response_class", None))
        import dataclasses as _dc

        _copy = {f.name for f in _dc.fields(Route)} - {
            "method", "path", "handler", "name"}
        for r in router.routes:
            kw = {k: getattr(r, k) for k in _copy
                  if k not in ("plan", "body", "body_kind")}
            if extra_deps:
                # Prepended → run before the route's own deps (FastAPI).
                kw["dependencies"] = list(extra_deps) + list(
                    kw.get("dependencies", []) or [])
            # Hierarchical default_response_class: the child route's own
            # response_class wins; otherwise the include-time default; the
            # parent router's default; finally None (App-level fallback).
            if kw.get("response_class") is None and default_rc is not None:
                kw["response_class"] = default_rc
            self.routes.append(Route(r.method, add_prefix + r.path,
                                     r.handler, r.name, **kw))


class Router:
    """App-wide matcher with a const-tier short-circuit (M3-R1).

    The hot path for static-shape routes (no `{param}` placeholders —
    /livez, /openapi.json, /docs, every parameter-less endpoint) is a
    single O(1) dict lookup. Routes that have a path-param placeholder
    fall through to the regex walk. Strict-synthesis ranks this 10-50×
    on static-route throughput (SYNTHESIS.md:31). User-code unchanged
    — the swap is purely an implementation detail of `match()`."""

    def __init__(self):
        self.routes: list[Route] = []
        # const-tier: keyed (method, path) → Route. Method-agnostic
        # 405 detection uses _const_paths for "is this path known at all".
        self._const: dict[tuple[str, str], Route] = {}
        self._const_paths: set[str] = set()
        # regex-tier: routes with `{param}` placeholders + WS routes.
        self._dyn: list[Route] = []

    def add(self, route: Route):
        self.routes.append(route)
        if route.is_ws:
            self._dyn.append(route)
            return
        # Static-shape routes have no path-param groups in their compiled
        # regex (Route.__post_init__ produces a re with named groupindex
        # exactly equal to the {param} count).
        if not route._rx.groupindex:
            self._const[(route.method, route.path)] = route
            self._const_paths.add(route.path)
        else:
            self._dyn.append(route)

    def match(self, method: str, path: str):
        method = method.upper()
        # const-tier: O(1) hit on static-shape routes.
        hit = self._const.get((method, path))
        if hit is not None:
            return hit, {}
        path_exists = path in self._const_paths
        # regex-tier: routes with path-param placeholders.
        for r in self._dyn:
            if r.is_ws:
                continue
            params = r.match(path)
            if params is None:
                continue
            path_exists = True
            if r.method == method:
                return r, params
        return (None, 405) if path_exists else (None, 404)

    def match_ws(self, path: str):
        for r in self._dyn:
            if not r.is_ws:
                continue
            params = r.match(path)
            if params is not None:
                return r, params
        return None, 404
