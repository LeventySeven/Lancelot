"""StaticFiles ASGI app — byte-faithful to Starlette 1.0.0 StaticFiles/
FileResponse for the covered subset (verified by
tests/test_m1_p6_byte_parity.py): md5(`{mtime}-{size}`) quoted ETag,
`formatdate(usegmt=True)` Last-Modified, realpath+commonpath traversal
guard, single byte-range (206 / 416), 304 (etag-only, no body), GET/HEAD
only. The Rust tower-http::fs / sendfile(2) realization
(src/static_files.rs, WBS U14) is the spec §3-69 unification; multi-range
/ directory-index / zero-copy sendfile are ledgered RED."""
import hashlib
import os
from email.utils import formatdate, parsedate
from mimetypes import guess_type

_304_ALLOW = {"cache-control", "content-location", "date", "etag",
              "expires", "vary"}


class StaticFiles:
    def __init__(self, *, directory: str):
        self.directory = os.path.realpath(directory)

    def _resolve(self, rel: str):
        full = os.path.realpath(os.path.join(self.directory, rel))
        if os.path.commonpath([full, self.directory]) != self.directory:
            return None  # traversal attempt — never escape the directory
        try:
            st = os.stat(full)
        except (FileNotFoundError, NotADirectoryError):
            return None
        if not os.path.isfile(full):
            return None
        return full, st

    @staticmethod
    def _not_modified(req_headers: dict, etag: str, last_mod: str) -> bool:
        inm = req_headers.get("if-none-match")
        if inm:
            return etag in [t.strip(" W/") for t in inm.split(",")]
        ims = req_headers.get("if-modified-since")
        if ims:
            a, b = parsedate(ims), parsedate(last_mod)
            if a is not None and b is not None and a >= b:
                return True
        return False

    async def __call__(self, scope, receive, send):
        async def plain(status, body=b"", extra=None):
            hdrs = list(extra or [])
            hdrs.append((b"content-length", str(len(body)).encode()))
            hdrs.append((b"content-type", b"text/plain; charset=utf-8"))
            await send({"type": "http.response.start", "status": status,
                        "headers": hdrs})
            await send({"type": "http.response.body", "body": body})

        method = scope["method"].upper()
        if method not in ("GET", "HEAD"):
            await plain(405, b"Method Not Allowed")
            return
        rel = scope["path"].lstrip("/")
        found = self._resolve(rel)
        if found is None:
            await plain(404, b"Not Found")
            return
        full, st = found
        size = st.st_size
        last_mod = formatdate(st.st_mtime, usegmt=True)
        etag = '"' + hashlib.md5(
            f"{st.st_mtime}-{st.st_size}".encode(),
            usedforsecurity=False).hexdigest() + '"'
        ctype = guess_type(full)[0] or "text/plain"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype = ctype + "; charset=utf-8"
        req_headers = {}
        for hk, hv in scope.get("headers", []):
            req_headers.setdefault(hk.decode().lower(), hv.decode())

        if self._not_modified(req_headers, etag, last_mod):
            # Starlette NotModifiedResponse: only the _304_ALLOW subset of
            # the FileResponse headers survives (here: etag), empty body.
            await send({"type": "http.response.start", "status": 304,
                        "headers": [(b"etag", etag.encode())]})
            await send({"type": "http.response.body", "body": b""})
            return

        rng = req_headers.get("range")
        common = [(b"accept-ranges", b"bytes"),
                  (b"last-modified", last_mod.encode()),
                  (b"etag", etag.encode())]
        with open(full, "rb") as fh:
            data = fh.read()

        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):].split(",")[0].strip()
            try:
                s_str, e_str = spec.split("-")
                start = int(s_str) if s_str else 0
                end = int(e_str) if e_str else size - 1
            except ValueError:
                await plain(400, b"Invalid Range")
                return
            if start >= size or start > end:  # unsatisfiable → 416
                await send({"type": "http.response.start", "status": 416,
                            "headers": [
                                (b"content-range",
                                 f"bytes */{size}".encode()),
                                (b"content-length", b"0"),
                                (b"content-type",
                                 b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": b""})
                return
            end = min(end, size - 1)
            chunk = data[start:end + 1]
            hdrs = list(common) + [
                (b"content-range",
                 f"bytes {start}-{end}/{size}".encode()),
                (b"content-length", str(len(chunk)).encode()),
                (b"content-type", ctype.encode())]
            await send({"type": "http.response.start", "status": 206,
                        "headers": hdrs})
            await send({"type": "http.response.body",
                        "body": b"" if method == "HEAD" else chunk})
            return

        hdrs = list(common) + [
            (b"content-length", str(size).encode()),
            (b"content-type", ctype.encode())]
        await send({"type": "http.response.start", "status": 200,
                    "headers": hdrs})
        await send({"type": "http.response.body",
                    "body": b"" if method == "HEAD" else data})
