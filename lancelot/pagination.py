"""M17 P9-cap — safe pagination / sort DEPENDENCIES (DoS-cap + sort-allowlist).

Lancelot's resilience-cap family (body cap, connection cap, per-route timeout,
rate-limit) had NO page-size cap — so a `?size=` query param (or the corpus
footgun `?items_per_page=-1` / `?limit=-1` -> unbounded full-table read) could
make a single request scan the whole table. This module closes that with two
small `Depends()`-style dependencies:

  * `PageParams` — parses `page`/`size` (or `limit`/`offset`) query params,
    CAPS size at `LANCELOT_PAGE_MAX_SIZE` (default 100), and has NO `-1` /
    unbounded escape hatch. Over-cap, zero, or negative -> HTTP 422.
  * `SortParam(allowed=[...])` — parses a `sort` query param against an
    explicit allowlist; anything not allowed -> 422 (anti sort-injection).
    A leading `-` denotes descending; the bare field name is still checked.

These return validated/capped scalars; **the user runs their own SQL** with
them (this is a byte-path/operator safety primitive, NOT an ORM and NOT a
response shape). PANEL FENCE: there is intentionally NO `Page[T]` response
envelope here — every corpus project ships a different one, `fastapi-pagination`
already exists, and an envelope has zero byte-path angle. Cap + allowlist only;
never sold as perf.

Usage:

    from lancelot import PageParams, SortParam, Depends

    @app.get("/items")
    def list_items(
        p=Depends(PageParams()),
        s=Depends(SortParam(allowed=["name", "created_at"])),
    ):
        # p.page / p.size / p.offset (or p.size==limit, p.offset in
        # limit_offset mode); s.field / s.descending — all validated/capped.
        rows = run_my_sql(limit=p.size, offset=p.offset,
                          order_by=s.field, desc=s.descending)
        return rows
"""
import os
from dataclasses import dataclass

from lancelot.exceptions import HTTPException
from lancelot.requests import Request

# The hard ceiling used when neither an explicit `max_size=` kwarg nor the
# LANCELOT_PAGE_MAX_SIZE env var is set. Deliberately conservative — a request
# should never be able to ask for an unbounded page.
DEFAULT_MAX_SIZE = 100
# The page size assumed when `size`/`limit` is omitted entirely.
DEFAULT_SIZE = 20


def _env_max_size() -> int:
    """Resolve the page-size cap at CALL time: explicit kwarg > env >
    DEFAULT_MAX_SIZE. Read per-call so a test/operator can flip the env
    without re-importing."""
    raw = os.environ.get("LANCELOT_PAGE_MAX_SIZE")
    if raw is None:
        return DEFAULT_MAX_SIZE
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SIZE
    return val if val >= 1 else DEFAULT_MAX_SIZE


def _parse_positive_int(raw, name: str, *, max_value: int):
    """Parse a query-string value to an int in [1, max_value]. Anything else
    — non-int, <= 0 (incl. the `-1` unbounded footgun), or > cap — raises 422.
    Returns the validated int. `raw` may be None (caller handles the default).
    """
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            422, f"query parameter {name!r} must be an integer")
    if val < 1:
        # This is where the `-1` / `0` full-table-read footgun dies.
        raise HTTPException(
            422, f"query parameter {name!r} must be >= 1 (got {val})")
    if val > max_value:
        raise HTTPException(
            422,
            f"query parameter {name!r} exceeds the maximum of {max_value} "
            f"(got {val})")
    return val


# The validated/capped pagination VALUE object handed to the route. NOT a
# response envelope (the panel fenced that) — deliberately NOT named `Page` so
# no envelope-looking symbol exists at the package surface (the fence test
# asserts `lancelot.pagination.Page` does not exist).
@dataclass
class PageValues:
    """In page/size mode: `page` (1-based), `size`, and the derived `offset` =
    (page-1)*size. In limit_offset mode: `size` holds the limit, `offset` holds
    the offset, `page` is derived (offset//size + 1). A VALUE object the user
    feeds to their own SQL, not a response shape."""
    page: int
    size: int
    offset: int


class PageParams:
    """A `Depends()`-style dependency that parses + caps pagination query
    params. Two styles:

      * style='page' (default): reads `page` (1-based, default 1) and `size`
        (default `DEFAULT_SIZE`). offset = (page-1)*size.
      * style='limit_offset': reads `limit` (default `DEFAULT_SIZE`, capped)
        and `offset` (default 0, floored at 0). `.size` aliases the limit.

    The size/limit is capped at `max_size` (explicit kwarg) else
    `LANCELOT_PAGE_MAX_SIZE` else `DEFAULT_MAX_SIZE`. There is NO unbounded
    escape: size/limit < 1 (incl. `-1`) or > cap -> HTTP 422.
    """

    # Exposed so a route/test can reference the omitted-size default.
    default_size = DEFAULT_SIZE

    def __init__(self, *, style: str = "page", max_size: int | None = None,
                 default_size: int = DEFAULT_SIZE):
        if style not in ("page", "limit_offset"):
            raise ValueError(
                "PageParams style must be 'page' or 'limit_offset'")
        self.style = style
        self._max_size = max_size
        self._default_size = default_size

    def _cap(self) -> int:
        return self._max_size if self._max_size is not None else _env_max_size()

    async def __call__(self, request: Request) -> "PageValues":
        q = request.query_params
        cap = self._cap()
        if self.style == "limit_offset":
            limit_raw = q.get("limit")
            size = (self._default_size if limit_raw is None
                    else _parse_positive_int(limit_raw, "limit",
                                             max_value=cap))
            offset_raw = q.get("offset")
            if offset_raw is None:
                offset = 0
            else:
                try:
                    offset = int(offset_raw)
                except (TypeError, ValueError):
                    raise HTTPException(
                        422, "query parameter 'offset' must be an integer")
                if offset < 0:
                    raise HTTPException(
                        422, "query parameter 'offset' must be >= 0 "
                             f"(got {offset})")
            page = (offset // size) + 1 if size else 1
            return PageValues(page=page, size=size, offset=offset)
        # page/size style.
        page_raw = q.get("page")
        page = (1 if page_raw is None
                else _parse_positive_int(page_raw, "page", max_value=2**31))
        size_raw = q.get("size")
        size = (self._default_size if size_raw is None
                else _parse_positive_int(size_raw, "size", max_value=cap))
        offset = (page - 1) * size
        return PageValues(page=page, size=size, offset=offset)


@dataclass
class Sort:
    """Validated sort values handed to the route: `field` (None if the param
    was omitted) and `descending`."""
    field: str | None
    descending: bool


class SortParam:
    """A `Depends()`-style dependency that parses a `sort` query param against
    an explicit allowlist. A leading `-` means descending; the bare field name
    is checked against `allowed`. A field not in `allowed` -> HTTP 422 (anti
    sort-injection — the user must NOT interpolate an arbitrary column into
    their ORDER BY). Omitting the param yields `field=None`.

    `param` selects the query-param name (default 'sort'); `default` is the
    field used when the param is omitted (still allowlist-checked at
    construction if non-None).
    """

    def __init__(self, *, allowed, param: str = "sort",
                 default: str | None = None):
        self.allowed = frozenset(allowed)
        if not self.allowed:
            raise ValueError("SortParam requires a non-empty `allowed` list")
        self.param = param
        if default is not None and default not in self.allowed:
            raise ValueError(
                f"SortParam default {default!r} is not in allowed {sorted(self.allowed)}")
        self.default = default

    async def __call__(self, request: Request) -> "Sort":
        raw = request.query_params.get(self.param)
        if raw is None or raw == "":
            if self.default is None:
                return Sort(field=None, descending=False)
            return Sort(field=self.default, descending=False)
        descending = False
        field = raw
        if field.startswith("-"):
            descending = True
            field = field[1:]
        elif field.startswith("+"):
            field = field[1:]
        if field not in self.allowed:
            raise HTTPException(
                422,
                f"sort field {field!r} is not allowed; choose one of "
                f"{sorted(self.allowed)}")
        return Sort(field=field, descending=descending)
