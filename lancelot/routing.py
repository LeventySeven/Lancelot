import dataclasses
import re
import typing
from dataclasses import dataclass
from typing import Any, Callable


def _is_body_annotation(ann) -> bool:
    """FastAPI body rule (deep/01 §3.1): a pydantic model, a dataclass, a
    bare `dict` annotation, a parameterized `dict[K, V]`, or a container
    (list/set/tuple/dict) whose element is a model → request body (not a
    scalar query param). Plain scalars/scalar-containers stay query."""
    if isinstance(ann, type) and hasattr(ann, "model_validate_json"):
        return True
    if dataclasses.is_dataclass(ann):
        return True
    # Bare `dict` (any JSON object body — FastAPI accepts JSON for this).
    if ann is dict:
        return True
    origin = typing.get_origin(ann)
    # Parameterized dict[K, V] is also a JSON object body (FastAPI parity:
    # test_typing_python39 posts `dict[str, list[int]]`).
    if origin is dict:
        return True
    if origin in (list, set, frozenset, tuple):
        return any(
            (isinstance(a, type) and hasattr(a, "model_validate_json"))
            or typing.get_origin(a) in (dict, list, tuple, set, frozenset)
            for a in typing.get_args(ann)
        )
    return False


@dataclass
# M6 T3.4 drop-in finding: FastAPI exposes the per-route record as
# `fastapi.routing.APIRoute`; Lancelot calls the equivalent class `Route`.
# Add the alias so `from lancelot.routing import APIRoute` works
# verbatim for real OSS apps that subclass APIRoute for custom dispatch
# (e.g. tiangolo's full-stack-fastapi-template).
class Route:
    method: str
    path: str
    handler: Callable[..., Any]
    name: str = ""
    include_in_schema: bool = True  # FastAPI: doc/openapi routes are False
    is_ws: bool = False  # WebSocket route (matched by scope type, not method)
    is_stream: bool = False  # @app.stream: yields lancelot.stream.Event
    stream_interval: int = 1   # Tier-A.A6: batch N events per SSE frame
    sse_keepalive: float = 15.0  # Tier-A.A5: SSE keepalive comment interval (s)
    timeout: float | None = None  # Tier-B.B5: per-route deadline → 504
    # FastAPI 0.110+ feature: reject JSON-body routes when the request's
    # Content-Type isn't JSON. None = inherit (router → app → True default).
    strict_content_type: bool | None = None
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
    # M15 T2 (#11): a dict shallow-merged into THIS route's OpenAPI
    # path-operation object (after the generated keys, so it can override).
    # FastAPI-named; build-time only (the schema is memoized) → zero hot path.
    openapi_extra: dict | None = None
    summary: str | None = None
    description: str | None = None
    tags: list | None = None
    dependencies: list | None = None
    deprecated: bool | None = None
    # M7 T3.5 (Veriff per-jurisdiction routing pattern): if set,
    # register this route ONLY when the active worker's LANCELOT_REGION
    # matches. None = register everywhere (the default — backwards-
    # compatible). The check happens at Router.add() time (registration),
    # not per-request, so there's zero hot-path overhead.
    region: str | None = None

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

        # FastAPI 0.115+ refuses Pydantic v1 BaseModel anywhere in the
        # route annotations. Detect it via `pydantic.v1.BaseModel` mro
        # check (no import-time pull — only check classes the user passes).
        def _is_pydantic_v1(t):
            if not isinstance(t, type):
                # Recurse into Union/Optional/list/etc.
                _o = typing.get_origin(t)
                if _o is not None:
                    return any(_is_pydantic_v1(a)
                               for a in typing.get_args(t))
                return False
            try:
                from pydantic.v1 import BaseModel as _V1BM
            except Exception:  # noqa: BLE001
                return False
            return issubclass(t, _V1BM)

        try:
            _all_hints = typing.get_type_hints(self.handler)
        except Exception:  # noqa: BLE001
            _all_hints = {}
        for _hn, _h in _all_hints.items():
            if _is_pydantic_v1(_h):
                from lancelot.exceptions import PydanticV1NotSupportedError

                raise PydanticV1NotSupportedError(
                    f"Pydantic v1 BaseModel is not supported in FastAPI "
                    f"route annotations (param {_hn!r}). Migrate to "
                    "Pydantic v2."
                )
        if self.response_model is not None and _is_pydantic_v1(
                self.response_model):
            from lancelot.exceptions import PydanticV1NotSupportedError

            raise PydanticV1NotSupportedError(
                "Pydantic v1 BaseModel is not supported as a "
                "response_model. Migrate to Pydantic v2."
            )
        if self.responses:
            from lancelot.exceptions import PydanticV1NotSupportedError

            for _k, _spec in self.responses.items():
                _m = (_spec or {}).get("model") if isinstance(
                    _spec, dict) else None
                if _m is not None and _is_pydantic_v1(_m):
                    raise PydanticV1NotSupportedError(
                        f"Pydantic v1 BaseModel is not supported in "
                        f"responses[{_k!r}].model. Migrate to Pydantic v2."
                    )
        # Validate response_model at REGISTRATION time (FastAPI parity):
        # a non-Pydantic-compatible type raises FastAPIError so the dev's
        # mistake surfaces immediately, not at first request.
        self._response_ta = None
        if self.response_model is not None:
            from lancelot.exceptions import FastAPIError

            try:
                from pydantic import TypeAdapter as _TA

                # P1: build the response_model adapter ONCE at registration and
                # KEEP it (was validated-then-discarded here, then rebuilt TWICE
                # per request in the ASGI seam — ~1.9µs/req on response_model
                # routes). TypeAdapter is stateless, so reuse is identical.
                self._response_ta = _TA(self.response_model)
            except Exception as _e:
                raise FastAPIError(
                    f"Invalid response_model {self.response_model!r}"
                ) from _e
        # FastAPI parity: if the handler's return annotation contains a
        # Starlette/Lancelot Response (or subclass), reject at registration.
        # Response isn't a valid response_model (can't validate against it).
        # The user should set `response_class=...` instead or return Response
        # directly without annotating.
        _ret_hint = None
        try:
            _ret_hint = typing.get_type_hints(self.handler).get("return")
        except Exception:  # noqa: BLE001
            _ret_hint = None
        if _ret_hint is not None:
            import types as _ts

            def _contains_response(t):
                # Optional[X]/Union[...]: scan args
                _o = typing.get_origin(t)
                if _o is typing.Union or _o is _ts.UnionType:
                    return any(_contains_response(a) for a in typing.get_args(t))
                if not isinstance(t, type):
                    return False
                # Lancelot's Response
                from lancelot.responses import Response as _LResp

                if issubclass(t, _LResp):
                    return True
                # Starlette's Response (without importing starlette ourselves)
                import sys as _sys

                _sm = _sys.modules.get("starlette.responses")
                if _sm is not None:
                    _sr = getattr(_sm, "Response", None)
                    if _sr is not None and issubclass(t, _sr):
                        return True
                return False

            # FastAPI behavior: if the WHOLE return annotation is a Response
            # subclass, auto-treat as response_model=None (no schema/validation).
            # If the return annotation is Union[Response, X] (ambiguous), THAT
            # is the only case we raise — same as FastAPI's actual error path.
            _ret_origin = typing.get_origin(_ret_hint)
            import types as _ts2
            _is_pure_response = (
                isinstance(_ret_hint, type)
                and _contains_response(_ret_hint)
            )
            _is_union_with_response = (
                (_ret_origin is typing.Union or _ret_origin is _ts2.UnionType)
                and _contains_response(_ret_hint)
            )

            if _is_pure_response:
                # M5 drop-in: match FastAPI's auto-coerce behavior; no error.
                if getattr(self, "response_model", None) is None:
                    self.response_model = None  # explicit no-schema marker
            elif _is_union_with_response:
                from lancelot.exceptions import FastAPIError

                raise FastAPIError(
                    f"Invalid args for response field! Hint: check that "
                    f"{_ret_hint!r} is a valid Pydantic field type. If you "
                    "are using a return type annotation that is not a "
                    "valid Pydantic field (e.g. Union[Response, dict]), "
                    "you can disable generating the response model from "
                    "the type annotation with `response_model=None`."
                )
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
        # FastAPI parity: a YIELD-dep with computed_scope="request" cannot
        # depend on a YIELD-dep with computed_scope="function" — the
        # outer's teardown lifetime would extend the inner's. Regular
        # (non-yield) deps have no teardown so their scope is moot.
        from lancelot.exceptions import FastAPIError as _SFE
        import inspect as _isp

        def _is_yield_dep(call):
            if call is None:
                return False
            # Peel functools.partial / @wraps / instance.__call__ to find
            # the actual function shape.
            import functools as _ft

            for _ in range(8):
                if isinstance(call, _ft.partial):
                    call = call.func
                    continue
                if hasattr(call, "__wrapped__") and call.__wrapped__ is not call:
                    call = call.__wrapped__
                    continue
                if not (_isp.isfunction(call) or _isp.ismethod(call)
                        or _isp.isclass(call) or _isp.isbuiltin(call)):
                    c = getattr(call, "__call__", None)
                    if c is None or c is call:
                        break
                    call = c
                    continue
                break
            return (_isp.isgeneratorfunction(call)
                    or _isp.isasyncgenfunction(call))

        def _walk(plan):
            for outer in plan.dependencies:
                if outer.plan is None:
                    continue
                outer_yield = _is_yield_dep(outer.call)
                outer_scope = "function" if (
                    getattr(outer, "_marker_scope", None) == "function"
                ) else "request"
                if outer_yield and outer_scope == "request":
                    for inner in outer.plan.dependencies:
                        inner_yield = _is_yield_dep(inner.call)
                        inner_scope = "function" if (
                            getattr(inner, "_marker_scope", None) == "function"
                        ) else "request"
                        if inner_yield and inner_scope == "function":
                            raise _SFE(
                                f'The dependency '
                                f'"{getattr(outer.call, "__name__", "?")}" '
                                f'has a scope of "{outer_scope}", it cannot '
                                f'depend on dependencies with scope '
                                f'"{inner_scope}"'
                            )
                _walk(outer.plan)
        _walk(self.plan)
        for i, dep in enumerate(self.dependencies or []):
            from lancelot.params import Depends as _Dep

            if not isinstance(dep, _Dep):
                continue
            sub_plan = compile_dependant(dep.dependency)
            # Route-level Security(dep, scopes=[...]) declarations also
            # propagate scopes to the dep's SecurityScopes injection.
            _route_scopes = tuple(getattr(dep, "scopes", ()) or ())
            self.plan.dependencies.append(SubDep(
                dep.dependency, sub_plan, dep.use_cache,
                f"__route_dep_{i}__", discard=True,
                scopes=_route_scopes,
                _marker_scope=getattr(dep, "scope", None)))
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
                # Unwrap Optional[X] / X | None first so the check fires
                # on `dict | None = Query(...)` too.
                import types as _types
                _u_ann = ann
                _u_origin = origin
                _u_args = args
                if _u_origin is typing.Union or _u_origin is _types.UnionType:
                    _non_none = [a for a in args if a is not type(None)]
                    if len(_non_none) == 1:
                        _u_ann = _non_none[0]
                        _u_origin = typing.get_origin(_u_ann) or _u_ann
                        _u_args = typing.get_args(_u_ann)
                if _u_origin in (list, tuple, set, frozenset) and _u_args:
                    inner = _u_args[0]
                    if (isinstance(inner, type)
                            and hasattr(inner, "model_validate")) \
                            or typing.get_origin(inner) is dict:
                        raise AssertionError(
                            f"{in_.title()} parameter '{fi.name}' must be "
                            "one of the supported types")
                elif _u_origin is dict or _u_ann is dict:
                    raise AssertionError(
                        f"{in_.title()} parameter '{fi.name}' must be "
                        "one of the supported types")
        self.body = None
        self.body_kind = "json"  # "json" | "form"  (multipart ledgered)
        # FastAPI body inference is method-aware: a bare `list[T]` /
        # `set[T]` / `tuple[T, ...]` on POST/PUT/PATCH/DELETE is a body;
        # on GET/HEAD/OPTIONS it stays query (the multi-value form).
        _body_methods = {"POST", "PUT", "PATCH", "DELETE"}
        _is_body_method = self.method in _body_methods
        for fi in list(self.plan.fields):
            ann = fi.annotation
            _origin = typing.get_origin(ann) or ann
            _is_param_container = _origin in (list, set, frozenset, tuple)
            # On NON-body methods, never lift a parameterized container
            # into the body — `list[T]` stays a multi-value query.
            if _is_param_container and not _is_body_method \
                    and not fi.explicit_marker:
                continue
            if _is_param_container and not _is_body_method:
                continue
            if not _is_body_annotation(ann):
                # Special case: on body-methods, a parameterized container
                # of scalars with NO explicit marker is body. Otherwise
                # skip.
                if _is_body_method and _is_param_container \
                        and not fi.explicit_marker:
                    pass  # fall through to body-lift below
                else:
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

    def get_route_handler(self):
        """FastAPI extension point: returns an `async (request) -> Response`
        callable. Custom `APIRoute` subclasses override this to wrap the
        original handler (logging, timing, custom error mapping, ...).

        The BASE implementation runs the full dispatch (body parse +
        dependency solve + handler + serialization) and returns a
        `lancelot.responses.Response`. On request-validation failure it
        RAISES `RequestValidationError` so a wrapper can catch it (the
        ASGI seam catches it otherwise and emits the canonical 422).

        This path is taken ONLY when `route_class` is customized — the
        common-case hot path in `asgi._http_dispatch` is untouched."""
        route = self

        async def _handler(request):
            from lancelot.di import solve_async
            from lancelot.exceptions import RequestValidationError
            from lancelot.responses import JSONResponse, Response
            from lancelot.validation import _wire_json

            # Body parse (mirrors the seam's pre-solve body handling).
            body_value, body_errors = None, []
            raw = bytes(getattr(request, "body", b"") or b"")
            if route.body is not None:
                bname, bval = route.body
                if raw:
                    if getattr(route, "body_kind", "json") == "form":
                        import urllib.parse as _up

                        pairs = _up.parse_qsl(raw.decode("latin-1"),
                                              keep_blank_values=True)
                        body_value, body_errors = bval.validate_form(pairs)
                    else:
                        body_value, body_errors = bval.validate_json(raw)
                elif getattr(route, "body_kind", "json") == "form":
                    body_value, body_errors = bval.validate_form([])
                else:
                    body_value, body_errors = bval.validate_json(b"{}")
            import contextlib as _cl

            fstack = _cl.AsyncExitStack()
            await fstack.__aenter__()
            try:
                async with solve_async(route.plan, request,
                                       _func_stack=fstack) as res:
                    errs = list(res.errors) + body_errors
                    if errs:
                        raise RequestValidationError(errs, body=raw)
                    kwargs = dict(res.kwargs)
                    if route.body is not None:
                        kwargs[route.body[0]] = body_value
                    import inspect as _isp

                    if route.plan.is_async:
                        result = await route.handler(**kwargs)
                    else:
                        from lancelot.pool import get_pool

                        result = await get_pool().offload(
                            route.handler, **kwargs)
                    await fstack.aclose()
            finally:
                if not fstack._exit_callbacks:  # already closed → no-op
                    pass
                try:
                    await fstack.aclose()
                except Exception:  # noqa: BLE001
                    pass
            # A handler that already returns a Response passes through.
            if isinstance(result, Response):
                return result
            payload = _wire_json(result)
            return JSONResponse(payload,
                                status_code=route.status_code or 200)

        return _handler


# M6 T3.4 drop-in compat: FastAPI's per-route record is `APIRoute` in
# `fastapi.routing`. We use `Route`. Alias so subclassing works for apps
# that customize dispatch via `APIRoute.get_route_handler()` override.
APIRoute = Route


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
        # FastAPI 0.110+ feature: None = inherit from parent (router
        # chain → app → True default).
        self.strict_content_type = kwargs.pop("strict_content_type", None)
        # Custom APIRoute subclass — routes registered on this router are
        # instantiated as `route_class` so a user's get_route_handler
        # override is honored (FastAPI custom-request-and-route feature).
        self.route_class = kwargs.pop("route_class", Route)
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
        _rc = self.route_class

        def deco(fn):
            self.routes.append(_rc(method, self.prefix + path, fn, **kw))
            return fn

        return deco

    def get(self, p, **kw): return self._add("GET", p, **kw)
    def post(self, p, **kw): return self._add("POST", p, **kw)
    def put(self, p, **kw): return self._add("PUT", p, **kw)
    def delete(self, p, **kw): return self._add("DELETE", p, **kw)
    def patch(self, p, **kw): return self._add("PATCH", p, **kw)
    def head(self, p, **kw): return self._add("HEAD", p, **kw)
    def options(self, p, **kw): return self._add("OPTIONS", p, **kw)
    def trace(self, p, **kw): return self._add("TRACE", p, **kw)

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
        # (extended), strict_content_type (propagated to child routes that
        # didn't set their own). The rest are surface-only.
        assert router is not self, (
            "Cannot include the same APIRouter instance into itself. "
            "Did you mean to include a different router?"
        )
        add_prefix = (self.prefix + prefix).rstrip("/")
        extra_deps = list(kwargs.pop("dependencies", None) or [])
        default_rc = kwargs.pop("default_response_class",
                                getattr(self, "default_response_class", None))
        # Resolve strict_content_type for the children: kwarg > child
        # router's own > this parent router's own > None (sentinel —
        # propagate up to App).
        sct_kw = kwargs.pop("strict_content_type", None)
        effective_sct = sct_kw if sct_kw is not None else \
            (getattr(router, "strict_content_type", None)
             if getattr(router, "strict_content_type", None) is not None
             else getattr(self, "strict_content_type", None))
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
            # Hierarchical strict_content_type: route's own > resolved
            # effective from chain. Stays None until the App.include_router
            # collapses to a concrete bool.
            if kw.get("strict_content_type") is None \
                    and effective_sct is not None:
                kw["strict_content_type"] = effective_sct
            self.routes.append(type(r)(r.method, add_prefix + r.path,
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
        # FastAPI: `app.router.route_class = MyAPIRoute` — routes added
        # via @app.get/etc. after this is set use the custom class so a
        # get_route_handler override is honored.
        self.route_class = Route

    def add(self, route: Route):
        # M7 T3.5 region gate: if this route is region-pinned and our
        # worker is in a different region, don't register it at all.
        # Zero hot-path cost — the runtime never sees the route.
        if route.region is not None:
            from lancelot.fleet import region as _current_region
            if route.region != _current_region():
                # Silently drop. The dual-region deploy registers BOTH
                # region's routes from the same code; each worker only
                # keeps its own region's set.
                return
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
