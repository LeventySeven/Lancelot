"""Minimal response objects so a handler can return pre-serialized content
(doc HTML, the openapi JSON) without the ASGI seam re-`_to_json`-ing it."""


class Response:
    media_type = "text/plain"

    def __init__(self, content=b"", status_code=200, media_type=None):
        self.status_code = status_code
        if media_type:
            self.media_type = media_type
        self.body = (
            content if isinstance(content, (bytes, bytearray))
            else str(content).encode()
        )


class HTMLResponse(Response):
    media_type = "text/html; charset=utf-8"


class JSONResponse(Response):
    media_type = "application/json"
