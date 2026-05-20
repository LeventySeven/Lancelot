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
