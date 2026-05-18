import re
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Route:
    method: str
    path: str
    handler: Callable[..., Any]
    name: str = ""

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
        from lancelot.validation import BodyValidator

        self.plan = compile_dependant(
            self.handler, path_params=set(self._rx.groupindex)
        )
        self.body = None
        for fi in list(self.plan.fields):
            ann = fi.annotation
            if isinstance(ann, type) and hasattr(ann, "model_validate_json"):
                self.body = (fi.name, BodyValidator(ann))
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

    def _add(self, method: str, path: str):
        def deco(fn):
            self.routes.append(Route(method, self.prefix + path, fn))
            return fn

        return deco

    def get(self, p): return self._add("GET", p)
    def post(self, p): return self._add("POST", p)
    def put(self, p): return self._add("PUT", p)
    def delete(self, p): return self._add("DELETE", p)
    def patch(self, p): return self._add("PATCH", p)
    def head(self, p): return self._add("HEAD", p)
    def options(self, p): return self._add("OPTIONS", p)

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
            params = r.match(path)
            if params is None:
                continue
            path_exists = True
            if r.method == method:
                return r, params
        return (None, 405) if path_exists else (None, 404)
