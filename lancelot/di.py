"""Two-phase DI. `compile_dependant`: signature → immutable DAG + flat
topological plan + interned cache slots, AT REGISTRATION (deep/01 §4, WBS
U15) — the hot path never introspects a signature. `solve`/`solve_async`
(Tasks 7-10) are execute-only: into Python only for the N user callables."""
import contextlib
import inspect
import typing
from dataclasses import dataclass

from lancelot.params import Depends, Required, extract_field
from lancelot.validation import compile_validator


@dataclass
class SubDep:
    call: typing.Callable
    plan: "Plan"
    use_cache: bool
    param_name: str  # parent param this dep binds to (recorded at COMPILE time
    #                  so solve() never re-introspects a signature — U15)
    discard: bool = False  # route-level deps: run for side-effects/validation,
    #                       result is NOT bound to a handler kwarg (FastAPI rule)
    scopes: tuple = ()  # scopes from a Security(dep, scopes=[...]) marker,
    #                      propagated to the dep's SecurityScopes injection
    _marker_scope: str | None = None  # Depends(scope=...) — "function" or
    #                      "request" (default). function-scope yield-deps
    #                      tear down BETWEEN handler-return and response.


@dataclass
class Plan:
    handler: typing.Callable
    fields: list  # list[FieldInfo] (validated params)
    validators: dict  # name -> ParamValidator
    dependencies: list  # list[SubDep], topological order
    is_async: bool
    security: list = None  # list[(SecurityBase, scopes_tuple)] for OpenAPI
    request_params: list = None  # param names bound to the live Request

    def __post_init__(self):
        if self.security is None:
            self.security = []
        if self.request_params is None:
            self.request_params = []

    def field(self, name):
        return next(f for f in self.fields if f.name == name)

    def uses_scopes(self) -> bool:
        """FastAPI-parity recursive check: this Plan (or anything it
        transitively depends on) consumes SecurityScopes. Drives the
        cache_key — if False, scopes are excluded so paths through the
        same dep share a cache entry (test_security_scopes_sub_dependency
        get_db_session: 1 call across both paths)."""
        if any(isinstance(e, tuple) and e[0] == "__sscopes__"
               for e in self.request_params):
            return True
        # An OpenAPI-tracked security scheme also "uses scopes".
        if self.security:
            return True
        for sub in self.dependencies:
            if sub.scopes:
                return True
            if sub.plan is not None and sub.plan.uses_scopes():
                return True
        return False


def _is_security(obj) -> bool:
    from lancelot.security import SecurityBase

    return isinstance(obj, SecurityBase)


def compile_dependant(fn: typing.Callable, path_params=()) -> Plan:
    # functools.partial unwrap — typing.get_type_hints doesn't accept
    # partial; FastAPI introspects the wrapped __func__ and pre-binds
    # the .args / .keywords.
    import functools as _ft

    partial_args = ()
    partial_kw: dict = {}
    orig_callable = fn  # used as the actual call target (partial preserves bind)
    if isinstance(fn, _ft.partial):
        partial_args = fn.args
        partial_kw = dict(fn.keywords or {})
        fn = fn.func
    # functools.lru_cache / functools.cache / contextlib.wraps decorators
    # store the wrapped fn on `__wrapped__`. inspect.signature would
    # otherwise see `(*args, **kwargs)` (the wrapper's signature) and
    # synthesize spurious query params. Peel until we bottom out.
    for _ in range(8):
        if hasattr(fn, "__wrapped__") and fn.__wrapped__ is not fn:
            fn = fn.__wrapped__
        else:
            break
    # Callable INSTANCE (FastAPI parity: `Depends(some_instance)`) — its
    # signature is its `__call__`. typing.get_type_hints requires a real
    # callable/class; introspect __call__ for instances.
    if not (inspect.isfunction(fn) or inspect.ismethod(fn)
            or inspect.isclass(fn) or inspect.isbuiltin(fn)):
        _call = getattr(fn, "__call__", None)
        if _call is not None:
            hints = typing.get_type_hints(_call, include_extras=True)
            sig = inspect.signature(_call)
        else:
            hints = {}
            sig = inspect.Signature()
    else:
        hints = typing.get_type_hints(fn, include_extras=True)
        sig = inspect.signature(fn)
    from lancelot.requests import Request as _Req

    # Skip params already pre-bound by functools.partial. Positional
    # partial_args bind in declaration order; partial_kw binds by name.
    _bound_names: set = set(partial_kw.keys())
    _param_names = [n for n in sig.parameters.keys()]
    for _i in range(len(partial_args)):
        if _i < len(_param_names):
            _bound_names.add(_param_names[_i])

    fields, validators, deps, security, req_params = [], {}, [], [], []
    for name, p in sig.parameters.items():
        if name in _bound_names:
            continue
        ann = hints.get(
            name, p.annotation if p.annotation is not inspect.Parameter.empty else str
        )
        # PEP 695 (Py 3.12+) / typing_extensions TypeAliasType: unwrap to
        # the underlying value so Depends/Query markers inside it become
        # visible. Same logic the params.extract_field path uses.
        _val = getattr(ann, "__value__", None)
        if _val is not None and type(ann).__name__ == "TypeAliasType":
            ann = _val
        if ann is _Req:  # `request: Request` → bound to the live Request
            req_params.append(name)
            continue
        from lancelot.background import BackgroundTasks as _BT

        if ann is _BT:  # FastAPI: a fresh BackgroundTasks injected per req
            req_params.append(("__bg__", name))
            continue
        from lancelot.websockets import WebSocket as _WS

        # `websocket: WebSocket` (FastAPI) OR a bare param literally named
        # `websocket` (Starlette WebSocketRoute convention) → live socket.
        if ann is _WS or (
            name == "websocket"
            and p.annotation is inspect.Parameter.empty
            and p.default is inspect.Parameter.empty
        ):
            req_params.append(("__ws__", name))
            continue
        from lancelot.responses import Response as _Resp

        # `response: Response` — FastAPI magic for user-mutable response
        # (set_cookie / headers / status_code). A fresh Response is built
        # per-request and merged into the actual emit at finalize.
        if isinstance(ann, type) and issubclass(ann, _Resp):
            req_params.append(("__resp__", name))
            continue
        from lancelot.security import SecurityScopes as _SS

        # `security_scopes: SecurityScopes` — FastAPI magic injectable.
        # NOT validated; the scopes union of enclosing Security() deps.
        if ann is _SS:
            req_params.append(("__sscopes__", name))
            continue
        default = p.default if p.default is not inspect.Parameter.empty else Required
        dep_marker = default if isinstance(default, Depends) else None
        # FastAPI registration-time ambiguity checks
        # (test_ambiguous_params parity).
        from lancelot.params import Param as _ParamCls, \
            Path as _PathCls

        _annotated_param_marker = None  # the Param() inside Annotated, if any
        _annotated_dep_marker = None  # the Depends() inside Annotated, if any
        if typing.get_origin(ann) is typing.Annotated:
            for m in typing.get_args(ann)[1:]:
                if isinstance(m, _ParamCls):
                    if _annotated_param_marker is None:
                        _annotated_param_marker = m
                    # Path(default=...) inside Annotated → FastAPI rule:
                    # "Path parameters cannot have a default value".
                    if isinstance(m, _PathCls) and m.default is not Required:
                        raise AssertionError(
                            "Path parameters cannot have a default value")
                    # Other Param()s with default=... inside Annotated are
                    # also invalid — must set default via `= …` outside.
                    if m.default is not Required:
                        raise AssertionError(
                            f"`{type(m).__name__}` default value cannot be "
                            f"set in `Annotated` for {name!r}. Set the "
                            "default value with `=` instead."
                        )
                if isinstance(m, Depends):
                    dep_marker = m
                    _annotated_dep_marker = m
        # Annotated Depends() AND `= Depends(...)` default: ambiguous.
        if _annotated_dep_marker is not None \
                and isinstance(p.default, Depends):
            raise AssertionError(
                f"Cannot specify `Depends` in `Annotated` and default "
                f"value together for {name!r}"
            )
        # Annotated Param() (Query, Header, ...) AND `= Depends(...)`:
        # ambiguous mix of body-param marker and DI marker.
        if _annotated_param_marker is not None \
                and isinstance(p.default, Depends):
            raise AssertionError(
                f"Cannot specify a FastAPI annotation in `Annotated` "
                f"and `Depends` as a default value together for {name!r}"
            )
        if dep_marker is not None:
            dep = dep_marker.dependency
            # FastAPI `Depends()` no-arg form: the param's annotation IS the
            # dep callable. The annotation is typically a class (Depends()
            # then instantiates it via the class's __init__ signature) or an
            # instance with __call__.
            if dep is None:
                # Strip Annotated to get the raw type.
                _raw = ann
                if typing.get_origin(_raw) is typing.Annotated:
                    _raw = typing.get_args(_raw)[0]
                dep = _raw
            # Scopes from Security(...) markers travel WITH the SubDep so
            # the sub-dep's SecurityScopes injection (if any) sees them
            # accumulated from every Security() on its caller chain.
            _dep_scopes = tuple(getattr(dep_marker, "scopes", ()) or ())
            # Depends(..., scope="function") asks for early teardown.
            _marker_scope = getattr(dep_marker, "scope", None)
            if _is_security(dep):
                # A security scheme is invoked directly with the Request —
                # NOT recursively compiled (its __call__(request) signature
                # is not a DI graph). Scopes (params.Security) are plumbed
                # for OpenAPI, NOT enforced (MUST-13).
                deps.append(SubDep(dep, None, dep_marker.use_cache, name,
                                   scopes=_dep_scopes,
                                   _marker_scope=_marker_scope))
                security.append((dep, _dep_scopes))
                continue
            sub = compile_dependant(dep, path_params)
            deps.append(SubDep(dep, sub, dep_marker.use_cache, name,
                               scopes=_dep_scopes,
                               _marker_scope=_marker_scope))
            continue
        fi = extract_field(name, ann, default, path_params)
        # Bucket-model: Query()/Header()/Cookie() + BaseModel annotation →
        # the WHOLE bucket is validated against the model (FastAPI parity);
        # `_raw_for` returns the full bucket dict, ParamValidator's existing
        # TypeAdapter(Model).validate_python(dict) does the rest.
        if fi.marker.in_ in ("query", "header", "cookie") \
                and isinstance(fi.annotation, type) \
                and hasattr(fi.annotation, "model_validate"):
            fi.is_bucket = fi.marker.in_
        fields.append(fi)
        validators[name] = compile_validator(fi)
    # FastAPI body rule (deep/01 §3.1.3): exactly ONE Body() scalar param,
    # no embed=True override → the raw JSON body IS the value (not an
    # object with that field name). Mark the field so _raw_for returns
    # the parsed body root instead of looking up a key in a dict.
    _body_fields = [f for f in fields if f.marker.in_ == "body"]
    if len(_body_fields) == 1 and not getattr(
            _body_fields[0].marker, "embed", False):
        _body_fields[0].body_scalar = True
    return Plan(orig_callable, fields, validators, deps,
                inspect.iscoroutinefunction(fn), security, req_params)


@dataclass
class SolveResult:
    kwargs: dict | None
    errors: list


_MISSING = object()


def _multi_to_model_dict(multi: dict, model) -> dict:
    """Bucket-model coercion: per-field, if the field is sequence-typed
    pass the full list of values; else pass the last. Extras with >1
    values stay as list (lossless; pydantic extra='allow' accepts it)."""
    import typing as _t

    seq = set()
    for fname, finfo in model.model_fields.items():
        keys = {fname}
        if finfo.alias:
            keys.add(finfo.alias)
        origin = _t.get_origin(finfo.annotation) or finfo.annotation
        if origin in (list, tuple, set, frozenset):
            seq.update(keys)
    return {k: (vs if (k in seq or len(vs) > 1) else vs[-1])
            for k, vs in multi.items()}


def _raw_for(fi, request):
    """Pull the raw value + presence for a field from its source bucket."""
    # Bucket-model fields take the WHOLE bucket dict, validated by the
    # field's existing TypeAdapter(Model).validate_python(dict).
    if fi.is_bucket == "query":
        multi = request.query_params_multi or {}
        return _multi_to_model_dict(multi, fi.annotation), bool(multi)
    if fi.is_bucket == "header":
        multi = request.headers_multi or {}
        lower = {k.lower(): vs for k, vs in multi.items()}
        return _multi_to_model_dict(lower, fi.annotation), bool(lower)
    if fi.is_bucket == "cookie":
        cks = request.cookies or {}
        multi = {k: [v] for k, v in cks.items()}
        return _multi_to_model_dict(multi, fi.annotation), bool(cks)
    src = fi.marker.in_
    # Pydantic v2 input-direction key: validation_alias > alias > name.
    key = fi.validation_alias or fi.alias or fi.name
    if src == "path":
        return request.path_params.get(key), key in request.path_params
    if src == "query":
        # Sequence-typed query params get the multi-value list (e.g.
        # `device_ids: list[int] = Query()` reads ?device_ids=1&device_ids=2
        # as [1, 2]); scalars get the last value (existing behavior).
        import typing as _t

        origin = _t.get_origin(fi.annotation) or fi.annotation
        if origin in (list, tuple, set, frozenset):
            multi = getattr(request, "query_params_multi", None) or {}
            return multi.get(key), key in multi
        return request.query_params.get(key), key in request.query_params
    if src == "header":
        conv = getattr(fi.marker, "convert_underscores", True)
        k = key.replace("_", "-").lower() if conv else key.lower()
        h = {kk.lower(): vv for kk, vv in request.headers.items()}
        return h.get(k), k in h
    if src == "cookie":
        return request.cookies.get(key), key in request.cookies
    if src == "body":
        # FastAPI: 2+ Body() params (or Body(embed=True) on a single one)
        # → JSON object keyed by name. EXACTLY ONE Body() param with no
        # explicit embed → raw JSON body IS the value (deep/01 §3.1.3).
        # The plan flagged it at compile time (di.compile_dependant) as
        # `fi.body_scalar`. Parse once, cache the parsed root.
        cache = getattr(request, "_body_obj_cache", _MISSING)
        if cache is _MISSING:
            import json as _json

            try:
                parsed = _json.loads(request.body) if request.body else _MISSING
            except Exception:  # noqa: BLE001 - malformed → field absent
                parsed = _MISSING
            request._body_obj_cache = parsed
            cache = parsed
        if getattr(fi, "body_scalar", False):
            # Raw body IS the value (or "absent" if the request had none).
            if cache is _MISSING:
                return None, False
            return cache, True
        # Embed / multi-Body path: look up by name in the JSON object.
        if not isinstance(cache, dict):
            return None, False
        return cache.get(key), key in cache
    if src == "file":
        # multipart/form-data — parsed once, cached on request.
        form = getattr(request, "_multipart_cache", None)
        if form is None:
            ct = ""
            for hk, hv in request.headers.items():
                if hk.lower() == "content-type":
                    ct = hv
                    break
            if "multipart/form-data" in ct:
                from lancelot.uploads import parse_multipart

                form = parse_multipart(request.body, ct)
            else:
                form = {}
            request._multipart_cache = form
        v = form.get(key)
        # FastAPI: `p: bytes = File()` reads the file bytes; `p:
        # UploadFile = File()` keeps the wrapper. List annotations get
        # the multi-value list; scalar annotations the last value.
        # Optional/Union[T, None] strips the None branch.
        from lancelot.uploads import UploadFile as _UF
        import typing as _t

        ann = fi.annotation
        # Unwrap Union[X, None] / X | None / Optional[X]
        import types as _types

        _origin = _t.get_origin(ann)
        if _origin is _t.Union or _origin is _types.UnionType:
            non_none = [a for a in _t.get_args(ann) if a is not type(None)]
            if len(non_none) == 1:
                ann = non_none[0]
        origin = _t.get_origin(ann) or ann
        is_seq = origin in (list, tuple, set, frozenset)
        if not is_seq and isinstance(v, list):
            v = v[-1]
        if is_seq and not isinstance(v, list):
            v = [v] if v is not None else []
        # Read UploadFile bytes when annotation is bytes / list[bytes]
        elem = _t.get_args(ann)[0] if (
            is_seq and _t.get_args(ann)) else ann
        if elem is bytes:
            if isinstance(v, list):
                v = [(x.file.seek(0) or x.file.read())
                     if isinstance(x, _UF) else x for x in v]
            elif isinstance(v, _UF):
                v.file.seek(0)
                v = v.file.read()
        return v, key in form
    if src == "form":
        # application/x-www-form-urlencoded via stdlib OR multipart/form-
        # data via python-multipart (mixed File + Form routes need the
        # multipart parse for Form fields too — file_and_form_order_9116).
        ct = ""
        for hk, hv in request.headers.items():
            if hk.lower() == "content-type":
                ct = hv
                break
        if "multipart/form-data" in ct:
            # Re-use the multipart cache (text parts become str values).
            mp = getattr(request, "_multipart_cache", None)
            if mp is None:
                from lancelot.uploads import parse_multipart

                mp = parse_multipart(request.body, ct)
                request._multipart_cache = mp
            v = mp.get(key)
            return v, key in mp
        form = getattr(request, "_form_cache", None)
        if form is None:
            import urllib.parse

            if "application/x-www-form-urlencoded" in ct:
                # Keep multi-value form; per-field below decides scalar/list
                # based on the FieldInfo annotation (FastAPI parity).
                form = urllib.parse.parse_qs(
                    request.body.decode("latin-1"))
            else:
                form = {}
            request._form_cache = form
        vs = form.get(key)
        if vs is None:
            return None, False
        import types as _types
        import typing as _t

        ann = fi.annotation
        # Unwrap Optional[X] / X | None / Union[X, None] so the list
        # detection works for the common `list[str] | None = None` form.
        _o = _t.get_origin(ann)
        if _o is _t.Union or _o is _types.UnionType:
            non_none = [a for a in _t.get_args(ann)
                        if a is not type(None)]
            if len(non_none) == 1:
                ann = non_none[0]
        origin = _t.get_origin(ann) or ann
        is_seq = origin in (list, tuple, set, frozenset)
        return (vs if is_seq else vs[-1]), True
    return request.body, bool(request.body)  # body handled at the ASGI seam (T11)


def _resolve_dep(sub: SubDep, request):
    """Apply request.app.dependency_overrides (deep/01 §4.4). The override's
    own signature is compiled once and memoized on a PER-APP store
    (request.app.override_plans) — never a process-global (no unbounded
    cross-app cache leak; A2). The use_cache key stays the ORIGINAL
    dependency (FastAPI caches by the original, not the override)."""
    app = getattr(request, "app", None)
    ov = getattr(app, "dependency_overrides", None) or {}
    target = ov.get(sub.call, sub.call)
    if target is sub.call:
        return target, sub.plan
    store = getattr(app, "override_plans", None)
    if store is None:  # bare/test app object without an override-plan store
        return target, compile_dependant(target)
    p = store.get(target)
    if p is None:
        p = compile_dependant(target)
        store[target] = p
    return target, p


def solve(plan: Plan, request, _cache=None) -> SolveResult:
    """Execute-only. Single dependency pass; shared `_cache` keyed by the
    original callable so a diamond sub-dep runs once per request (use_cache).
    Accumulates ALL errors (deep/01 §0.5/§0.8) — deps depth-first, then this
    plan's params in declared order — never fail-fast."""
    cache = _cache if _cache is not None else {}
    kwargs, errors = {}, []
    for sub in plan.dependencies:
        if sub.use_cache and sub.call in cache:
            if not sub.discard:
                kwargs[sub.param_name] = cache[sub.call]
            continue
        target, subplan = _resolve_dep(sub, request)
        if _is_security(target):
            raise RuntimeError(
                "security schemes require the async request path (solve_async)")
        sr = solve(subplan, request, cache)
        if sr.errors:
            errors.extend(sr.errors)
            continue
        val = target(**sr.kwargs)
        if sub.use_cache:
            cache[sub.call] = val
        if not sub.discard:
            kwargs[sub.param_name] = val
    for fi in plan.fields:
        raw, present = _raw_for(fi, request)
        value, errs = plan.validators[fi.name].validate(raw, present=present)
        if errs:
            errors.extend(errs)
        else:
            kwargs[fi.name] = value
    _bind_req_params(plan, request, kwargs)
    return SolveResult(None, errors) if errors else SolveResult(kwargs, [])


def _bind_req_params(plan, request, kwargs, parent_scopes=()):
    """Bind the magic-injectable params (Request / BackgroundTasks /
    WebSocket / Response / SecurityScopes) into `kwargs`.

    `parent_scopes`: scopes from the Security(this_dep, scopes=[...]) marker
    on the CALLER of this plan. They flow into SecurityScopes injection
    (FastAPI parity — security_scopes.scopes accumulates from the chain)."""
    for entry in plan.request_params:
        if isinstance(entry, tuple):
            kind, name = entry
            if kind == "__bg__":
                kwargs[name] = request._background
            elif kind == "__ws__":
                kwargs[name] = request._ws
            elif kind == "__resp__":
                # Lazily create a per-request scratch Response. The ASGI
                # seam reads it after the handler to merge cookies/headers/
                # status_code into the actual emit. NOT visible to user
                # if they never touch it (FastAPI parity).
                from lancelot.responses import Response as _Resp

                scratch = getattr(request, "_resp_scratch", None)
                if scratch is None:
                    # FastAPI scratch semantics: status_code=None means the
                    # user did NOT assign one; route.status_code (or 200)
                    # then wins. The user assigning later replaces it.
                    scratch = _Resp()
                    scratch.status_code = None
                    request._resp_scratch = scratch
                kwargs[name] = scratch
            elif kind == "__sscopes__":
                from lancelot.security import SecurityScopes as _SS

                # SecurityScopes accumulates: this plan's own security
                # tuples + every parent Security(...) declaration that
                # routed through this dep. parent_scopes is passed by
                # solve_async from the calling SubDep.scopes.
                all_scopes = list(parent_scopes)
                for _scheme, _scopes in plan.security:
                    all_scopes.extend(_scopes)
                kwargs[name] = _SS(all_scopes)
        else:
            kwargs[entry] = request


def _is_gen_dep(call):
    return inspect.isgeneratorfunction(call) or inspect.isasyncgenfunction(call)


@contextlib.asynccontextmanager
async def solve_async(plan: Plan, request, _cache=None, _stack=None,
                      _func_stack=None, _parent_scopes=()):
    """Like solve(), but `yield`-dependencies are entered as (async) context
    managers on a SHARED AsyncExitStack so their teardown runs AFTER the
    response, in reverse (LIFO) order — PEP-789 / deep/01 §5. The recursive
    calls share the owner's stack; only the owner enters/exits it (so
    teardown happens once, after the `async with solve_async(...)` block).

    `_func_stack`: separate AsyncExitStack for scope="function" yield-deps.
    These tear down BETWEEN handler-return and response-emit (FastAPI
    semantics — `Depends(dep, scope="function")` is a request-scoped
    resource that doesn't live through the response). The seam closes
    this stack explicitly after the handler returns; falls back to the
    main `stack` only when no _func_stack is provided (back-compat).

    `_parent_scopes`: scopes from a Security(plan, scopes=[...]) marker on
    the CALLER. Flow into SecurityScopes injection for this plan."""
    own = _stack is None
    stack = _stack or contextlib.AsyncExitStack()
    cache = _cache if _cache is not None else {}
    if own:
        await stack.__aenter__()
    try:
        kwargs, errors = {}, []
        for sub in plan.dependencies:
            # FastAPI Dependant.cache_key: (call, sorted_security_scopes,
            # computed_scope). Scopes are in the key ONLY when this dep
            # (or any of its transitive sub-deps) actually consumes them
            # (`_uses_scopes` — FastAPI parity). Test refs:
            #   test_security_scopes_sub_dependency_caching:
            #     get_db_session: _uses_scopes=False → cached once across
            #     paths; get_user_me (depends on get_current_user which
            #     takes SecurityScopes): _uses_scopes=True → cached per
            #     accumulated scope tuple → 2 calls on 2 paths.
            # The dep's `computed_scope` (Depends(scope="function")) is
            # ALSO part of the cache key — same callable with different
            # lifetimes is a DIFFERENT dependency
            # (test_dependency_yield_scope::test_two_scopes).
            _sub_plan = sub.plan
            _accum_scopes = tuple(_parent_scopes) + tuple(sub.scopes)
            _uses_scopes = bool(_sub_plan and _sub_plan.uses_scopes()) \
                or bool(sub.scopes)
            # Normalize: scope=None / scope="request" / "" all alias to
            # "request" (the default) so `Depends(d)` and `Depends(d,
            # scope="request")` share a cache entry. ONLY scope="function"
            # is distinct (it has a different lifetime → different
            # session/resource). Matches FastAPI's Dependant.computed_scope.
            _norm_scope = "function" if (
                getattr(sub, "_marker_scope", None) == "function") else "request"
            _scoped_key = (sub.call,
                           tuple(sorted(_accum_scopes))
                           if _uses_scopes else (),
                           _norm_scope)
            if sub.use_cache and _scoped_key in cache:
                if not sub.discard:
                    kwargs[sub.param_name] = cache[_scoped_key]
                continue
            target, subplan = _resolve_dep(sub, request)
            if _is_security(target):
                # Invoke the scheme directly with the live Request. Its 401
                # HTTPException propagates out through this async-with to the
                # ASGI seam (mapped byte-faithfully); the exc-info also
                # reaches yield-dep teardown (B4-correct).
                val = target(request)
                if inspect.isawaitable(val):
                    val = await val
                if sub.use_cache:
                    cache[_scoped_key] = val
                if not sub.discard:
                    kwargs[sub.param_name] = val
                continue
            # SecurityScopes accumulates DOWN the chain: grandparent's
            # scopes + this Security() declaration's scopes flow into the
            # sub-dep's injection. (Test ref: GH fastapi#5623 — siblings
            # see ONLY the route → caller path, not each other's scopes.)
            async with solve_async(
                    subplan, request, cache, stack, _func_stack,
                    _parent_scopes=tuple(_parent_scopes) + tuple(sub.scopes)
                    ) as sr:
                if sr.errors:
                    errors.extend(sr.errors)
                    continue
                # Callable INSTANCES (`obj()` where obj.__call__ is the
                # actual function): inspect checks return False for the
                # instance itself but True for its __call__. For
                # functools.partial, the dispatch type lives on `.func`.
                # Both can chain (`partial(instance)`) so we peel until we
                # bottom out on something inspect can introspect.
                import functools as _ft

                def _unwrap(x):
                    for _ in range(8):  # depth-limited; cycles impossible
                        if isinstance(x, _ft.partial):
                            x = x.func
                            continue
                        if (inspect.isfunction(x) or inspect.ismethod(x)
                                or inspect.isclass(x)
                                or inspect.isbuiltin(x)):
                            return x
                        c = getattr(x, "__call__", None)
                        if c is None or c is x:
                            return x
                        x = c
                    return x

                _probe = _unwrap(target)
                # scope="function" yield-deps register on the FUNCTION
                # stack so they tear down BETWEEN handler-return and
                # response-emit (FastAPI parity). All others stay on the
                # request stack and tear down AFTER the response.
                _scope = getattr(sub, "_marker_scope", None)
                _target_stack = _func_stack if (
                    _scope == "function" and _func_stack is not None
                ) else stack
                if inspect.isasyncgenfunction(_probe):
                    val = await _target_stack.enter_async_context(
                        contextlib.asynccontextmanager(target)(**sr.kwargs)
                    )
                elif inspect.isgeneratorfunction(_probe):
                    # stdlib @contextmanager.__exit__ does gen.throw(exc) on
                    # error → the dep's `except:`/`finally:` rollback runs (B4).
                    val = _target_stack.enter_context(
                        contextlib.contextmanager(target)(**sr.kwargs)
                    )
                elif inspect.iscoroutinefunction(_probe):
                    val = await target(**sr.kwargs)
                else:
                    # Plain sync dep → bounded blocking pool, never the
                    # loop (FastAPI solve_dependencies uses run_in_threadpool
                    # for sync deps too). Generator/async-gen yield-deps
                    # above stay on the loop so the AsyncExitStack /
                    # gen.throw teardown semantics (M1-P2 B4) are preserved.
                    from lancelot.pool import get_pool

                    val = await get_pool().offload(target, **sr.kwargs)
            if sub.use_cache:
                cache[_scoped_key] = val
            if not sub.discard:
                kwargs[sub.param_name] = val
        for fi in plan.fields:
            raw, present = _raw_for(fi, request)
            value, errs = plan.validators[fi.name].validate(raw, present=present)
            if errs:
                errors.extend(errs)
            else:
                kwargs[fi.name] = value
        _bind_req_params(plan, request, kwargs,
                         parent_scopes=_parent_scopes)
        yield SolveResult(None, errors) if errors else SolveResult(kwargs, [])
    except BaseException as exc:
        # B4: the handler (or a dep) raised inside the `async with` body.
        # Feed the REAL exc_info to the stack so each yield-dep's generator
        # gets the exception thrown in at its `yield` (its `except:`/`finally:`
        # rollback runs — PEP-789 / deep/01 §5).
        # FastAPI parity: a yield-dep that CATCHES the exception but doesn't
        # re-raise it is a documented anti-pattern. `__aexit__` returning
        # True means "exception suppressed by a dep"; FastAPI raises
        # FastAPIError pointing the user at the docs instead of letting
        # the swallow go silent.
        if own:
            suppressed = await stack.__aexit__(
                type(exc), exc, exc.__traceback__)
            if suppressed:
                from lancelot.exceptions import FastAPIError

                raise FastAPIError(
                    "Caught handled exception, but while raising an "
                    "exception and a dependency with yield, the "
                    "dependency must `raise` the same exception. Read "
                    "more about it in the docs: "
                    "https://fastapi.tiangolo.com/tutorial/dependencies/"
                    "dependencies-with-yield/"
                    "#dependencies-with-yield-and-except"
                ) from exc
        raise
    else:
        if own:
            await stack.__aexit__(None, None, None)  # teardown AFTER response
