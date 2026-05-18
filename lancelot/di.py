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
    cache_slot: int
    param_name: str  # parent param this dep binds to (recorded at COMPILE time
    #                  so solve() never re-introspects a signature — U15)


@dataclass
class Plan:
    handler: typing.Callable
    fields: list  # list[FieldInfo] (validated params)
    validators: dict  # name -> ParamValidator
    dependencies: list  # list[SubDep], topological order
    is_async: bool

    def field(self, name):
        return next(f for f in self.fields if f.name == name)


def compile_dependant(fn: typing.Callable, _slots=None) -> Plan:
    slots = _slots if _slots is not None else [0]
    hints = typing.get_type_hints(fn, include_extras=True)
    sig = inspect.signature(fn)
    fields, validators, deps = [], {}, []
    for name, p in sig.parameters.items():
        ann = hints.get(
            name, p.annotation if p.annotation is not inspect.Parameter.empty else str
        )
        default = p.default if p.default is not inspect.Parameter.empty else Required
        dep_marker = default if isinstance(default, Depends) else None
        if typing.get_origin(ann) is typing.Annotated:
            for m in typing.get_args(ann)[1:]:
                if isinstance(m, Depends):
                    dep_marker = m
        if dep_marker is not None:
            sub = compile_dependant(dep_marker.dependency, slots)
            slot = slots[0]
            slots[0] += 1
            deps.append(
                SubDep(dep_marker.dependency, sub, dep_marker.use_cache, slot, name)
            )
            continue
        fi = extract_field(name, ann, default)
        fields.append(fi)
        validators[name] = compile_validator(fi)
    return Plan(fn, fields, validators, deps, inspect.iscoroutinefunction(fn))


@dataclass
class SolveResult:
    kwargs: dict | None
    errors: list


def _raw_for(fi, request):
    """Pull the raw value + presence for a field from its source bucket."""
    src = fi.marker.in_
    key = fi.alias or fi.name
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
    return request.body, bool(request.body)  # body handled at the ASGI seam (T11)


_OVERRIDE_PLANS: dict = {}


def _resolve_dep(sub: SubDep, request):
    """Apply request.app.dependency_overrides (deep/01 §4.4). The override's
    own signature is compiled once (memoized). The use_cache key stays the
    ORIGINAL dependency (FastAPI caches by the original, not the override)."""
    ov = getattr(getattr(request, "app", None), "dependency_overrides", None) or {}
    target = ov.get(sub.call, sub.call)
    if target is sub.call:
        return target, sub.plan
    p = _OVERRIDE_PLANS.get(target)
    if p is None:
        p = compile_dependant(target)
        _OVERRIDE_PLANS[target] = p
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
    return SolveResult(None, errors) if errors else SolveResult(kwargs, [])


def _is_gen_dep(call):
    return inspect.isgeneratorfunction(call) or inspect.isasyncgenfunction(call)


@contextlib.asynccontextmanager
async def _sync_gen_cm(genfn, /, **kw):
    gen = genfn(**kw)
    val = next(gen)
    try:
        yield val
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


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
            async with solve_async(subplan, request, cache, stack) as sr:
                if sr.errors:
                    errors.extend(sr.errors)
                    continue
                if _is_gen_dep(target):
                    cm = (
                        contextlib.asynccontextmanager(target)
                        if inspect.isasyncgenfunction(target)
                        else _sync_gen_cm
                    )
                    arg = (target,) if cm is _sync_gen_cm else ()
                    val = await stack.enter_async_context(cm(*arg, **sr.kwargs))
                else:
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
        yield SolveResult(None, errors) if errors else SolveResult(kwargs, [])
    finally:
        if own:
            await stack.__aexit__(None, None, None)  # teardown AFTER response
