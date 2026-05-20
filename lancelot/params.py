"""FastAPI-compatible parameter markers. Required-ness is decided by the
DEFAULT, not the type (deep/01 §1.1). `Annotated[T, Query(...)]` is the
modern form; a bare default `= Query(...)` is the legacy form (both supported)."""
import typing
from dataclasses import dataclass


class _Sentinel:
    def __init__(self, name):
        self._n = name

    def __repr__(self):
        return self._n


Required = _Sentinel("Required")  # FastAPI's PydanticUndefined analogue
Default = _Sentinel("Default")  # "no explicit marker" sentinel

_CONSTRAINT_KEYS = (
    "gt", "ge", "lt", "le", "min_length", "max_length", "pattern",
    "multiple_of", "max_digits", "decimal_places",
    "allow_inf_nan",  # pydantic v2 numeric flag (FastAPI passes it through)
)


class Param:
    in_: str = "query"

    def __init__(self, default=Required, *, alias=None,
                 validation_alias=None, **constraints):
        self.default = default
        self.alias = alias
        # Pydantic v2 distinction: alias = input AND output; validation_alias
        # = input ONLY (errors loc still uses name/alias, not validation_alias).
        self.validation_alias = validation_alias
        self.constraints = {
            k: v for k, v in constraints.items()
            if k in _CONSTRAINT_KEYS and v is not None
        }


class Query(Param):
    in_ = "query"


class Path(Param):
    in_ = "path"

    def __init__(self, default=Required, **kw):
        super().__init__(default, **kw)


class Header(Param):
    in_ = "header"

    def __init__(self, default=Required, *, convert_underscores=True, **kw):
        super().__init__(default, **kw)
        self.convert_underscores = convert_underscores


class Cookie(Param):
    in_ = "cookie"


class Body(Param):
    in_ = "body"

    def __init__(self, default=Required, *, embed=False, **kw):
        # Pop body-only kwargs before super; super ignores unknowns via
        # **constraints filtering, but we want explicit storage for embed.
        super().__init__(default, **kw)
        # FastAPI Body(embed=True): scalar param is wrapped in a JSON
        # object keyed by the param name (or alias). Multiple embeds in
        # one route share the same object. Default behavior (embed=False)
        # treats the body as the raw scalar JSON.
        self.embed = embed


class Form(Body):
    in_ = "form"


class File(Body):
    in_ = "file"


@dataclass
class Depends:
    dependency: typing.Optional[typing.Callable] = None  # FastAPI allows
    # Depends() with no arg (uses the param's annotation type as dep callable)
    use_cache: bool = True
    scope: typing.Any = None  # FastAPI security-scopes wiring; ledgered

    def __hash__(self):
        # FastAPI parity: tools (e.g. fastapi-utils) need Depends/Security
        # to be hashable so they can dedupe them in a set. Two Depends()
        # are equal iff they reference the same dep + same caching/scope.
        return hash((type(self), self.dependency, self.use_cache,
                     self.scope))


@dataclass
class Security(Depends):
    scopes: tuple = ()

    def __hash__(self):
        return hash((type(self), self.dependency, self.use_cache,
                     self.scope, tuple(self.scopes)))


@dataclass
class FieldInfo:
    name: str
    annotation: typing.Any
    required: bool
    default: typing.Any
    marker: Param
    alias: str | None = None
    is_bucket: str = ""  # "" | "query" | "header" | "cookie" — bucket-model
    explicit_marker: bool = False  # True if user wrote Query()/Body()/etc.
    validation_alias: str | None = None  # input-only alias (Pydantic v2)
    body_scalar: bool = False  # single-Body-scalar pattern: raw body IS value


def _scalar_default_marker(annotation) -> Param:
    return Query()  # bare scalar with no marker defaults to Query (deep/01 §1)


def extract_field(name: str, annotation, default, path_params=()) -> FieldInfo:
    marker = None
    real_ann = annotation
    explicit = False
    # PEP 695 (Py 3.12+) type aliases: `type Foo = Annotated[int, Depends(...)]`
    # or `TypeAliasType("Foo", Annotated[...])` from typing_extensions.
    # Unwrap to the underlying value so Depends/Query/etc. are visible.
    _val = getattr(annotation, "__value__", None)
    if _val is not None and type(annotation).__name__ == "TypeAliasType":
        annotation = _val
    if typing.get_origin(annotation) is typing.Annotated:
        args = typing.get_args(annotation)
        inner = args[0]
        # Python flattens nested Annotated, so `Annotated[Json[list[str]],
        # Header()]` arrives as args = (list[str], Json, Header()). We
        # must PRESERVE non-Param/non-Depends metadata (e.g. Pydantic
        # `Json` marker, `StringConstraints`, ...) on the underlying
        # type — dropping it loses the Json-parse-then-validate behavior.
        preserved = []
        for meta in args[1:]:
            if isinstance(meta, (Param, Depends)):
                marker = meta
                explicit = True
            else:
                preserved.append(meta)
        real_ann = typing.Annotated[(inner, *preserved)] if preserved else inner
    if isinstance(default, (Param, Depends)):
        marker = default
        default = marker.default if isinstance(marker, Param) else Required
        explicit = True
    if marker is None:
        # FastAPI rule: a bare scalar whose NAME is a route path placeholder
        # is implicitly a Path param; otherwise Query (deep/01 §1).
        marker = Path() if name in path_params else _scalar_default_marker(real_ann)
    eff_default = default
    if isinstance(marker, Param) and marker.default is not Required:
        eff_default = marker.default
    required = eff_default is Required
    return FieldInfo(
        name, real_ann, required, None if required else eff_default,
        marker, getattr(marker, "alias", None),
        explicit_marker=explicit,
        validation_alias=getattr(marker, "validation_alias", None),
    )
