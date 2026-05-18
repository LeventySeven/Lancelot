"""Two-phase DI. `compile_dependant`: signature → immutable DAG + flat
topological plan + interned cache slots, AT REGISTRATION (deep/01 §4, WBS
U15) — the hot path never introspects a signature. `solve`/`solve_async`
(Tasks 7-10) are execute-only: into Python only for the N user callables."""
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
                SubDep(dep_marker.dependency, sub, dep_marker.use_cache, slot)
            )
            continue
        fi = extract_field(name, ann, default)
        fields.append(fi)
        validators[name] = compile_validator(fi)
    return Plan(fn, fields, validators, deps, inspect.iscoroutinefunction(fn))
