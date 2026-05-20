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


def _is_security(obj) -> bool:
    from lancelot.security import SecurityBase

    return isinstance(obj, SecurityBase)


def compile_dependant(fn: typing.Callable, path_params=()) -> Plan:
    hints = typing.get_type_hints(fn, include_extras=True)
    sig = inspect.signature(fn)
    from lancelot.requests import Request as _Req

    fields, validators, deps, security, req_params = [], {}, [], [], []
    for name, p in sig.parameters.items():
        ann = hints.get(
            name, p.annotation if p.annotation is not inspect.Parameter.empty else str
        )
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
        default = p.default if p.default is not inspect.Parameter.empty else Required
        dep_marker = default if isinstance(default, Depends) else None
        if typing.get_origin(ann) is typing.Annotated:
            for m in typing.get_args(ann)[1:]:
                if isinstance(m, Depends):
                    dep_marker = m
        if dep_marker is not None:
            dep = dep_marker.dependency
            if _is_security(dep):
                # A security scheme is invoked directly with the Request —
                # NOT recursively compiled (its __call__(request) signature
                # is not a DI graph). Scopes (params.Security) are plumbed
                # for OpenAPI, NOT enforced (MUST-13).
                scopes = tuple(getattr(dep_marker, "scopes", ()) or ())
                deps.append(SubDep(dep, None, dep_marker.use_cache, name))
                security.append((dep, scopes))
                continue
            sub = compile_dependant(dep, path_params)
            deps.append(SubDep(dep, sub, dep_marker.use_cache, name))
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
    return Plan(fn, fields, validators, deps,
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
        return request.query_params.get(key), key in request.query_params
    if src == "header":
        conv = getattr(fi.marker, "convert_underscores", True)
        k = key.replace("_", "-").lower() if conv else key.lower()
        h = {kk.lower(): vv for kk, vv in request.headers.items()}
        return h.get(k), k in h
    if src == "cookie":
        return request.cookies.get(key), key in request.cookies
    if src == "body" and getattr(fi.marker, "embed", False):
        # FastAPI Body(embed=True): the request body is a JSON object
        # keyed by the param name/alias; the handler receives the unwrapped
        # value. Parse once, cache on the request.
        cache = getattr(request, "_body_obj_cache", _MISSING)
        if cache is _MISSING:
            import json as _json

            try:
                parsed = _json.loads(request.body) if request.body else None
            except Exception:  # noqa: BLE001 - malformed → field absent
                parsed = None
            cache = parsed if isinstance(parsed, dict) else None
            request._body_obj_cache = cache
        if cache is None:
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
        return form.get(key), key in form
    if src == "form":
        # application/x-www-form-urlencoded only (stdlib — no runtime dep).
        # multipart/form-data + UploadFile is ledgered (needs
        # python-multipart). Parsed once per request, cached.
        form = getattr(request, "_form_cache", None)
        if form is None:
            import urllib.parse

            ct = ""
            for hk, hv in request.headers.items():
                if hk.lower() == "content-type":
                    ct = hv
                    break
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
        import typing as _t

        ann = fi.annotation
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
        kwargs[sub.param_name] = val
    for fi in plan.fields:
        raw, present = _raw_for(fi, request)
        value, errs = plan.validators[fi.name].validate(raw, present=present)
        if errs:
            errors.extend(errs)
        else:
            kwargs[fi.name] = value
    for entry in plan.request_params:
        if isinstance(entry, tuple) and entry[0] == "__bg__":
            kwargs[entry[1]] = request._background  # set by the ASGI seam
        elif isinstance(entry, tuple) and entry[0] == "__ws__":
            kwargs[entry[1]] = request._ws  # live WebSocket (ws seam)
        else:
            kwargs[entry] = request
    return SolveResult(None, errors) if errors else SolveResult(kwargs, [])


def _is_gen_dep(call):
    return inspect.isgeneratorfunction(call) or inspect.isasyncgenfunction(call)


@contextlib.asynccontextmanager
async def solve_async(plan: Plan, request, _cache=None, _stack=None):
    """Like solve(), but `yield`-dependencies are entered as (async) context
    managers on a SHARED AsyncExitStack so their teardown runs AFTER the
    response, in reverse (LIFO) order — PEP-789 / deep/01 §5. The recursive
    calls share the owner's stack; only the owner enters/exits it (so
    teardown happens once, after the `async with solve_async(...)` block)."""
    own = _stack is None
    stack = _stack or contextlib.AsyncExitStack()
    cache = _cache if _cache is not None else {}
    if own:
        await stack.__aenter__()
    try:
        kwargs, errors = {}, []
        for sub in plan.dependencies:
            if sub.use_cache and sub.call in cache:
                kwargs[sub.param_name] = cache[sub.call]
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
                    cache[sub.call] = val
                kwargs[sub.param_name] = val
                continue
            async with solve_async(subplan, request, cache, stack) as sr:
                if sr.errors:
                    errors.extend(sr.errors)
                    continue
                if inspect.isasyncgenfunction(target):
                    val = await stack.enter_async_context(
                        contextlib.asynccontextmanager(target)(**sr.kwargs)
                    )
                elif inspect.isgeneratorfunction(target):
                    # stdlib @contextmanager.__exit__ does gen.throw(exc) on
                    # error → the dep's `except:`/`finally:` rollback runs (B4).
                    val = stack.enter_context(
                        contextlib.contextmanager(target)(**sr.kwargs)
                    )
                elif inspect.iscoroutinefunction(target):
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
                cache[sub.call] = val
            kwargs[sub.param_name] = val
        for fi in plan.fields:
            raw, present = _raw_for(fi, request)
            value, errs = plan.validators[fi.name].validate(raw, present=present)
            if errs:
                errors.extend(errs)
            else:
                kwargs[fi.name] = value
        for entry in plan.request_params:
            if isinstance(entry, tuple) and entry[0] == "__bg__":
                kwargs[entry[1]] = request._background  # set by ASGI seam
            elif isinstance(entry, tuple) and entry[0] == "__ws__":
                kwargs[entry[1]] = request._ws  # live WebSocket (ws seam)
            else:
                kwargs[entry] = request
        yield SolveResult(None, errors) if errors else SolveResult(kwargs, [])
    except BaseException as exc:
        # B4: the handler (or a dep) raised inside the `async with` body.
        # Feed the REAL exc_info to the stack so each yield-dep's generator
        # gets the exception thrown in at its `yield` (its `except:`/`finally:`
        # rollback runs — PEP-789 / deep/01 §5), then re-raise.
        if own:
            await stack.__aexit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        if own:
            await stack.__aexit__(None, None, None)  # teardown AFTER response
