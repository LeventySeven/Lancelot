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
)


class Param:
    in_: str = "query"

    def __init__(self, default=Required, *, alias=None, **constraints):
        self.default = default
        self.alias = alias
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


class Form(Body):
    in_ = "form"


class File(Body):
    in_ = "file"


@dataclass
class Depends:
    dependency: typing.Callable
    use_cache: bool = True


@dataclass
class Security(Depends):
    scopes: tuple = ()


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


def _scalar_default_marker(annotation) -> Param:
    return Query()  # bare scalar with no marker defaults to Query (deep/01 §1)


def extract_field(name: str, annotation, default, path_params=()) -> FieldInfo:
    marker = None
    real_ann = annotation
    explicit = False
    if typing.get_origin(annotation) is typing.Annotated:
        args = typing.get_args(annotation)
        real_ann = args[0]
        for meta in args[1:]:
            if isinstance(meta, (Param, Depends)):
                marker = meta
                explicit = True
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
    )
