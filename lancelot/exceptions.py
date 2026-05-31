class FastAPIError(Exception):
    """Framework-level errors (FastAPI parity name)."""


class PydanticV1NotSupportedError(FastAPIError):
    """FastAPI 0.115+ refuses Pydantic v1 BaseModel anywhere in route
    annotations (param, response_model, return type, responses[*]['model']).
    Surface this with a clear, parity-named error."""


class RequestValidationError(ValueError):
    """422-shape validation error (FastAPI parity name)."""

    def __init__(self, errors, *, body=None):
        self._errors = errors
        self.body = body
        super().__init__(str(errors))

    def errors(self):
        return self._errors


class WebSocketRequestValidationError(RequestValidationError):
    """WS-1008 validation error (deep/01 MUST-7)."""


class ResponseValidationError(ValueError):
    """500-shape response validation error (FastAPI parity name). Raised
    when response_model coercion fails — distinct from request validation
    (422) so middlewares/handlers can differentiate."""

    def __init__(self, errors, *, body=None):
        self._errors = errors
        self.body = body
        super().__init__(str(errors))

    def errors(self):
        return self._errors


class HTTPException(Exception):
    """FastAPI/Starlette-shape HTTP error. The ASGI seam maps it to
    JSONResponse({"detail": detail}, status_code, headers) byte-faithfully
    (deep/03 §1 MUST-11)."""

    def __init__(self, status_code: int, detail=None, headers: dict | None = None):
        self.status_code = status_code
        self.detail = detail if detail is not None else ""
        self.headers = headers or {}
        super().__init__(f"{status_code}: {self.detail}")


PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"


def _status_phrase(status: int) -> str:
    """Reason phrase for an HTTP status (RFC 9457 §4.1 says `title` SHOULD
    be the status phrase when omitted). Falls back to a generic label."""
    try:
        import http

        return http.HTTPStatus(status).phrase
    except Exception:  # noqa: BLE001
        return "Error"


class ProblemDetail(HTTPException):
    """OPT-IN RFC 9457 (`application/problem+json`) error. The user EXPLICITLY
    raises this (or returns `.to_response()`) — it is NOT the default error
    shape. Lancelot's default `HTTPException` still emits the byte-identical
    FastAPI `{"detail": ...}` JSON (the conformance contract is untouched).

    RFC 9457 members:
      - `type`   — a URI reference identifying the problem type
                   (defaults to `"about:blank"`).
      - `title`  — a short human-readable summary (defaults to the status
                   phrase, e.g. "Not Found").
      - `status` — the HTTP status code (also the response status).
      - `detail` — a human-readable explanation specific to THIS occurrence.
      - `instance` — a URI reference identifying the specific occurrence.
    Any additional keyword data goes in `extensions` (RFC 9457 §3.2
    extension members) and is merged into the top-level object.

    Subclasses `HTTPException` so a `raise ProblemDetail(...)` is caught by
    the same seam path; the seam detects the subclass and emits
    `application/problem+json` (with the RFC body) instead of `{"detail"}`.
    A user-registered exception handler still wins (FastAPI parity)."""

    def __init__(self, status: int = 500, *, type: str = "about:blank",
                 title: str | None = None, detail=None,
                 instance: str | None = None,
                 headers: dict | None = None,
                 extensions: dict | None = None):
        # status doubles as the HTTP status code; detail keeps the
        # HTTPException attribute contract (str-able).
        super().__init__(status, detail, headers)
        self.type = type
        self.title = title if title is not None else _status_phrase(status)
        self.instance = instance
        self.extensions = dict(extensions or {})

    def problem_dict(self) -> dict:
        """The RFC 9457 problem object as a plain dict (extension members
        merged at the top level). `detail`/`instance` are omitted when
        unset so the object stays minimal."""
        obj: dict = {
            "type": self.type,
            "title": self.title,
            "status": self.status_code,
        }
        if self.detail not in (None, ""):
            obj["detail"] = self.detail
        if self.instance is not None:
            obj["instance"] = self.instance
        # Extension members never clobber the standard ones.
        for k, v in self.extensions.items():
            obj.setdefault(k, v)
        return obj

    def to_response(self):
        """A ready-to-return Response carrying this problem as
        `application/problem+json` (the RETURN path; the raise path is
        handled by the seam). Carries any headers set on the problem."""
        from lancelot.responses import JSONResponse

        resp = JSONResponse(self.problem_dict(), status_code=self.status_code,
                            media_type=PROBLEM_JSON_MEDIA_TYPE)
        if self.headers:
            # Merge problem headers (e.g. Retry-After) onto the response.
            for k, v in self.headers.items():
                resp.headers[str(k).lower()] = v
        return resp


def retry_taxonomy(target, status: int, *, retry_after=None):
    """Convenience: set client-retry hints on `target` (anything with a
    `.headers` dict — a Response OR a ProblemDetail) from a small status
    taxonomy, so callers (esp. SDKs) back off correctly instead of falling
    back to their own status-code heuristics.

    Taxonomy:
      - 429 / 503  → retryable (`X-Should-Retry: true`) + a `Retry-After`
        header (the explicit `retry_after` seconds, else a sane default).
      - other 5xx  → retryable; no `Retry-After` mandated (set one
        explicitly via `retry_after=` if you have a backoff hint).
      - 4xx (client errors, non-429) → NOT retryable (`false`) — retrying a
        bad request just wastes a round trip.

    Mirrors `Response.set_retry_hint`'s `X-Should-Retry` wire format and is
    the documented pairing for surfacing upstream-LLM backpressure (e.g. a
    rate-limited `@app.batch` upstream) without a thundering-herd retry."""
    headers = getattr(target, "headers", None)
    if headers is None:
        raise TypeError(
            "retry_taxonomy(target, ...) needs a Response/ProblemDetail "
            "with a .headers dict")
    if status == 429 or status == 503:
        should_retry = True
        ra = retry_after if retry_after is not None else (1 if status == 503 else 1)
        headers["retry-after"] = str(int(ra))
    elif status >= 500:
        should_retry = True
        if retry_after is not None:
            headers["retry-after"] = str(int(retry_after))
    else:
        should_retry = False
        if retry_after is not None:
            headers["retry-after"] = str(int(retry_after))
    headers["x-should-retry"] = "true" if should_retry else "false"
    return target
