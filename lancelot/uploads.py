"""M1-EXIT-X5 — minimum multipart/form-data + UploadFile. `python-
multipart` is lazy-imported only on the first File()-route call: runtime
deps stay `pydantic` only; users who want file uploads `pip install
python-multipart` (same opt-in pattern as `httpx` for `lancelot.llm`).

`UploadFile` wraps a `SpooledTemporaryFile`; `bytes = File()` reads the
file fully into memory (FastAPI parity for the small-file case)."""
import tempfile


class UploadFile:
    """Minimal UploadFile (FastAPI/Starlette-shape): exposes filename,
    content_type, size, sync read/seek/close. Wraps a SpooledTemporary
    File so small uploads stay in-memory; spills to disk past the
    threshold."""

    spool_max_size = 1024 * 1024  # 1 MiB before disk spill

    def __init__(self, file=None, *, filename: str | None = None,
                 content_type: str | None = None, size: int | None = None):
        self.filename = filename
        self.content_type = content_type or "application/octet-stream"
        self.size = size
        self.file = file or tempfile.SpooledTemporaryFile(
            max_size=self.spool_max_size)

    async def read(self, n: int = -1) -> bytes:
        return self.file.read(n) if n >= 0 else self.file.read()

    async def seek(self, offset: int) -> None:
        self.file.seek(offset)

    async def close(self) -> None:
        self.file.close()

    async def write(self, data: bytes) -> None:
        self.file.write(data)

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        # Tell pydantic this is an opaque object — no validation needed.
        # The OpenAPI schema for File() params is set separately
        # (type:string, format:binary) by the openapi synth path.
        from pydantic_core import core_schema as _cs

        return _cs.is_instance_schema(cls)

    @classmethod
    def __get_pydantic_json_schema__(cls, schema, handler):
        return {"type": "string", "format": "binary"}


def parse_multipart(body: bytes, content_type: str) -> dict:
    """Parse a multipart/form-data body using python-multipart (lazy-
    imported). Returns {field_name: value-or-UploadFile, ...} matching
    FastAPI's FormData shape (single value per key for now; multi-value
    is on the M1-EXIT roadmap)."""
    try:
        from python_multipart import parse_options_header, MultipartParser
    except ImportError as e:
        raise RuntimeError(
            "File/multipart requires `python-multipart` "
            "(pip install python-multipart)") from e

    ct, options = parse_options_header(content_type)
    boundary = options.get(b"boundary")
    if not boundary:
        return {}
    result: dict = {}
    current_field: dict | None = None

    def on_part_begin():
        nonlocal current_field
        current_field = {"name": None, "filename": None,
                         "content_type": None, "data": bytearray()}

    def on_header_field(data, start, end):
        current_field["_hf"] = bytes(data[start:end]).lower()

    def on_header_value(data, start, end):
        hf = current_field.get("_hf", b"")
        val = bytes(data[start:end])
        if hf == b"content-disposition":
            # Cheap parser: name="...", filename="..."
            for kv in val.split(b";"):
                kv = kv.strip()
                if kv.startswith(b'name="') and kv.endswith(b'"'):
                    current_field["name"] = kv[6:-1].decode()
                elif kv.startswith(b'filename="') and kv.endswith(b'"'):
                    current_field["filename"] = kv[10:-1].decode()
        elif hf == b"content-type":
            current_field["content_type"] = val.decode()

    def on_part_data(data, start, end):
        current_field["data"].extend(data[start:end])

    def on_part_end():
        f = current_field
        if f["name"] is None:
            return
        if f["filename"] is not None:
            uf = UploadFile(filename=f["filename"],
                            content_type=f["content_type"],
                            size=len(f["data"]))
            uf.file.write(bytes(f["data"]))
            uf.file.seek(0)
            result[f["name"]] = uf
        else:
            result[f["name"]] = bytes(f["data"]).decode("utf-8", "replace")

    parser = MultipartParser(boundary, callbacks={
        "on_part_begin": on_part_begin,
        "on_header_field": on_header_field,
        "on_header_value": on_header_value,
        "on_part_data": on_part_data,
        "on_part_end": on_part_end,
    })
    parser.write(body)
    parser.finalize()
    return result
