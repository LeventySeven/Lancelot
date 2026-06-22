"""M20 — the NATIVE `app.run()` server emits handler-returned Response objects.

THE BUG (real-server, cardinal rule #2): the native Rust value path was
body-only + assumed JSON — it ran `serialize::to_json_bytes` on WHATEVER the
handler returned, with no `Response` detection and nowhere to carry a status /
content-type / headers. A handler returning `Response`/`JSONResponse`/
`HTMLResponse` (which ALL builtin probes do — `/livez`, `/readyz`,
`/fleet/status`, `/openapi.json`, `/docs`) hit `to_json_bytes` on a
non-JSON-serializable object → raise → 500. Green on TestClient/ASGI (which
emits Response objects correctly), broken only on the native server — so the
proof MUST run over a real socket against `_native.run`, NOT TestClient.

Asserts on the native server:
 (a) builtin `GET /livez` → 200, application/json, real probe body;
 (b) builtin `GET /readyz` → 200, application/json (ready once started);
 (c) a user handler returning `JSONResponse(..., status_code=201)` → 201 +
     application/json + the right JSON body;
 (d) a user handler returning `HTMLResponse("<b>hi</b>", status_code=418)`
     → 418 + text/html content-type + the HTML body;
 (e) a plain dict return STAYS byte-identical (200, application/json) — the
     hot path is unchanged;
 (f) a Response carrying an extra header / cookie surfaces it on the wire.
"""
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _spawn(src, port, extra_env=None):
    env = {**os.environ, "LANCELOT_HOST": "127.0.0.1"}
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "-c", src, str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def _wait_listen(proc, port, deadline_s=12):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited rc={proc.returncode}: "
                f"{proc.stderr.read().decode()[:1200]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not start listening in time")


# A server whose handlers exercise every Response shape the native value path
# must now carry: a builtin probe (JSONResponse via the framework), an explicit
# JSONResponse with a non-200 status, an HTMLResponse with a non-200 status +
# text/html content-type, a plain dict (the unchanged hot path), and a Response
# carrying an extra header.
_SERVER = r"""
import sys
import lancelot
from lancelot.responses import JSONResponse, HTMLResponse, PlainTextResponse

app = lancelot.App()

@app.get("/created")
async def created(req):
    return JSONResponse({"made": True}, status_code=201)

@app.get("/html")
async def html(req):
    return HTMLResponse("<b>hi</b>", status_code=418)

@app.get("/plain-dict")
async def plain_dict(req):
    # The unchanged hot path: a plain value → 200 application/json.
    return {"ok": True, "n": 7}

@app.get("/with-header")
async def with_header(req):
    r = PlainTextResponse("body", status_code=200)
    r.headers["x-custom"] = "lancelot"
    r.set_cookie("sid", "abc123")
    return r

app.run(host="127.0.0.1", port=int(sys.argv[1]))
"""


def test_native_emits_response_objects():
    port = _free_port()
    proc = _spawn(_SERVER, port, {"LANCELOT_DRAIN_SECS": "3"})
    try:
        _wait_listen(proc, port)
        base = f"http://127.0.0.1:{port}"

        # (a) builtin probe — the headline regression. JSONResponse via the
        # framework; used to 500.
        r = httpx.get(f"{base}/livez", timeout=5)
        assert r.status_code == 200, (r.status_code, r.text)
        assert "application/json" in r.headers["content-type"], r.headers
        assert r.json().get("status") == "ok", r.json()

        # (b) /readyz — ready once started.
        r = httpx.get(f"{base}/readyz", timeout=5)
        assert r.status_code == 200, (r.status_code, r.text)
        assert "application/json" in r.headers["content-type"], r.headers
        assert r.json().get("status") == "ok", r.json()

        # (c) user JSONResponse(status_code=201).
        r = httpx.get(f"{base}/created", timeout=5)
        assert r.status_code == 201, (r.status_code, r.text)
        assert "application/json" in r.headers["content-type"], r.headers
        assert r.json() == {"made": True}, r.json()

        # (d) user HTMLResponse(status_code=418) — text/html, NOT json.
        r = httpx.get(f"{base}/html", timeout=5)
        assert r.status_code == 418, (r.status_code, r.text)
        assert "text/html" in r.headers["content-type"], r.headers
        assert r.text == "<b>hi</b>", r.text

        # (e) plain dict STAYS byte-identical (200 application/json).
        r = httpx.get(f"{base}/plain-dict", timeout=5)
        assert r.status_code == 200, (r.status_code, r.text)
        assert "application/json" in r.headers["content-type"], r.headers
        assert r.json() == {"ok": True, "n": 7}, r.json()

        # (f) a Response's extra header + Set-Cookie reach the wire.
        r = httpx.get(f"{base}/with-header", timeout=5)
        assert r.status_code == 200, (r.status_code, r.text)
        assert "text/plain" in r.headers["content-type"], r.headers
        assert r.headers.get("x-custom") == "lancelot", r.headers
        assert "sid=abc123" in r.headers.get("set-cookie", ""), r.headers
        assert r.text == "body", r.text
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_streamingresponse_to_native_raises_not_silent_empty():
    """M1 (recheck): a StreamingResponse (a Response subclass whose content
    lives in body_iterator, not .body) must NOT silently render an empty body
    on the native value path — `_render_native` raises a clear error so it
    fails loud (→ 500 + logged) instead of serving a silent empty 200. Native
    streaming is @app.stream's job."""
    from lancelot.responses import StreamingResponse

    sr = StreamingResponse(iter([b"a", b"b"]))
    with pytest.raises(TypeError):
        sr._render_native()
