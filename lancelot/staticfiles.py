"""StaticFiles ASGI app — byte-faithful to Starlette 1.0.0 StaticFiles/
FileResponse for the covered subset (verified by
tests/test_m1_p6_byte_parity.py incl. ordered raw-ASGI headers): md5(
`{mtime}-{size}`) quoted ETag, `formatdate(usegmt=True)` Last-Modified,
realpath+commonpath traversal guard, single byte-range incl. RFC suffix
`bytes=-N` (206 / 400 malformed / 416 unsatisfiable, Starlette's exact
messages), 304 (NotModifiedResponse allow-set, no body), GET/HEAD only,
FileResponse header order (content-type FIRST). The Rust tower-http::fs /
sendfile(2) realization (src/static_files.rs, WBS U14) is the spec §3-69
unification; multi-range / directory-index are ledgered RED."""
import asyncio
import hashlib
import os
from email.utils import formatdate, parsedate
from mimetypes import guess_type

# Starlette NotModifiedResponse keeps only this subset of the response
# headers on a 304 (here, of the FileResponse headers, only `etag`).
_304_ALLOW = {"cache-control", "content-location", "date", "etag",
              "expires", "vary"}


class _Malformed(Exception):
    def __init__(self, content="Malformed range header."):
        self.content = content


class _NotSatisfiable(Exception):
    def __init__(self, max_size):
        self.max_size = max_size


def _parse_ranges(range_: str, size: int):
    # Faithful to starlette.responses.FileResponse._parse_ranges.
    out = []
    for part in range_.split(","):
        part = part.strip()
        if not part or part == "-" or "-" not in part:
            continue
        s_str, e_str = part.split("-", 1)
        s_str, e_str = s_str.strip(), e_str.strip()
        try:
            start = int(s_str) if s_str else size - int(e_str)
            end = (int(e_str) + 1 if s_str and e_str and int(e_str) < size
                   else size)
            out.append((start, end))
        except ValueError:
            continue
    return out


def _parse_range_header(http_range: str, size: int):
    try:
        units, range_ = http_range.split("=", 1)
    except ValueError:
        raise _Malformed()
    if units.strip().lower() != "bytes":
        raise _Malformed("Only support bytes range")
    ranges = _parse_ranges(range_, size)
    if len(ranges) == 0:
        raise _Malformed("Range header: range must be requested")
    if any(not (0 <= s < size) for s, _ in ranges):
        raise _NotSatisfiable(size)
    if any(s > e for s, e in ranges):
        raise _Malformed("Range header: start must be less than end")
    return ranges


class StaticFiles:
    def __init__(self, *, directory: str):
        self.directory = os.path.realpath(directory)

    def _resolve(self, rel: str):
        try:
            full = os.path.realpath(os.path.join(self.directory, rel))
            if os.path.commonpath([full, self.directory]) != self.directory:
                return None  # traversal — never escape the directory
            st = os.stat(full)
        except (FileNotFoundError, NotADirectoryError, ValueError, OSError):
            # NUL byte / bad path → os.stat raises ValueError; Starlette
            # 404s the same input (must NOT escape the ASGI callable).
            return None
        if not os.path.isfile(full):
            return None
        return full, st

    @staticmethod
    def _not_modified(req_headers, etag, last_mod):
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
            # PlainTextResponse wire order: [<extra>, content-length,
            # content-type LAST] (Starlette init_headers).
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
        found = self._resolve(scope["path"].lstrip("/"))
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
            # Starlette NotModifiedResponse: only _304_ALLOW headers of the
            # FileResponse set survive (here: etag), empty body.
            fr_headers = {"accept-ranges": b"bytes",
                          "last-modified": last_mod.encode(),
                          "etag": etag.encode(), "content-type": ctype.encode()}
            kept = [(k.encode(), v) for k, v in fr_headers.items()
                    if k in _304_ALLOW]
            await send({"type": "http.response.start", "status": 304,
                        "headers": kept})
            await send({"type": "http.response.body", "body": b""})
            return

        # Read off the event loop: a synchronous read of a large static file
        # would stall the reactor for every concurrent request (Starlette uses
        # anyio.open_file; asyncio.to_thread is the stdlib, zero-dep equivalent).
        def _read():
            with open(full, "rb") as fh:
                return fh.read()

        data = await asyncio.to_thread(_read)
        rng = req_headers.get("range")
        # Starlette FileResponse raw header order: content-type FIRST,
        # then accept-ranges, content-length, last-modified, etag
        # (content-range appended LAST on 206).
        if rng is not None:
            try:
                ranges = _parse_range_header(rng, size)
            except _Malformed as exc:
                await plain(400, exc.content.encode())
                return
            except _NotSatisfiable as exc:
                await send({"type": "http.response.start", "status": 416,
                            "headers": [
                                (b"content-range",
                                 f"bytes */{exc.max_size}".encode()),
                                (b"content-length", b"0"),
                                (b"content-type",
                                 b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": b""})
                return
            if len(ranges) == 1:  # multi-range is ledgered (§3-69) → 200
                start, end = ranges[0]
                chunk = data[start:end]
                hdrs = [
                    (b"content-type", ctype.encode()),
                    (b"accept-ranges", b"bytes"),
                    (b"content-length", str(end - start).encode()),
                    (b"last-modified", last_mod.encode()),
                    (b"etag", etag.encode()),
                    (b"content-range",
                     f"bytes {start}-{end - 1}/{size}".encode())]
                await send({"type": "http.response.start", "status": 206,
                            "headers": hdrs})
                await send({"type": "http.response.body",
                            "body": b"" if method == "HEAD" else chunk})
                return

        hdrs = [
            (b"content-type", ctype.encode()),
            (b"accept-ranges", b"bytes"),
            (b"content-length", str(size).encode()),
            (b"last-modified", last_mod.encode()),
            (b"etag", etag.encode())]
        await send({"type": "http.response.start", "status": 200,
                    "headers": hdrs})
        await send({"type": "http.response.body",
                    "body": b"" if method == "HEAD" else data})
