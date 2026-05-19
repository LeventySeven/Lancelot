class HTTPException(Exception):
    """FastAPI/Starlette-shape HTTP error. The ASGI seam maps it to
    JSONResponse({"detail": detail}, status_code, headers) byte-faithfully
    (deep/03 §1 MUST-11)."""

    def __init__(self, status_code: int, detail=None, headers: dict | None = None):
        self.status_code = status_code
        self.detail = detail if detail is not None else ""
        self.headers = headers or {}
        super().__init__(f"{status_code}: {self.detail}")
