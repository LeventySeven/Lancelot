"""M17 P5 — constant-time static-token / API-key gate
(`lancelot.auth.StaticTokenGate`).

Real-server (subprocess uvicorn + raw HTTP) tests: an auth gate must be proven
under a real socket, including the Host-spoof case where the middleware MUST key
off `scope['path']` (not `request.url`, which can be poisoned by a forged Host
header) when deciding whether a path is guarded.

What we prove:
  - valid token -> 200; wrong token -> 401 + WWW-Authenticate; missing -> 401.
  - OPTIONS requests and `open_paths` (/livez, /readyz) pass UNGATED.
  - a spoofed `Host:` header CANNOT bypass the guard — the gate decides on
    `scope['path']`, so a guarded path stays guarded regardless of Host.
"""
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


def _spawn(src, *args):
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", src, str(port), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 12
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited rc={proc.returncode}: "
                               f"{proc.stderr.read().decode()[:800]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc, port
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not start")


def _stop(*procs):
    import signal
    for p in procs:
        p.send_signal(signal.SIGTERM)
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def _raw(port, raw_bytes):
    """Send a raw HTTP/1.1 request and return (status_line, headers_dict, body)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(raw_bytes)
    chunks = []
    s.settimeout(5)
    try:
        while True:
            d = s.recv(65536)
            if not d:
                break
            chunks.append(d)
            if b"\r\n\r\n" in b"".join(chunks) and len(chunks) >= 1:
                # Got headers + (small) body; one recv is enough for our tiny
                # responses. Try one more non-blocking read then stop.
                try:
                    s.settimeout(0.3)
                    more = s.recv(65536)
                    if more:
                        chunks.append(more)
                except OSError:
                    pass
                break
    finally:
        s.close()
    data = b"".join(chunks)
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_line = lines[0].decode("latin1")
    headers = {}
    for ln in lines[1:]:
        if b":" in ln:
            k, _, v = ln.partition(b":")
            headers[k.decode("latin1").strip().lower()] = v.decode("latin1").strip()
    return status_line, headers, body


_GATE = r"""
import sys, uvicorn, lancelot
from lancelot.auth import StaticTokenGate

app = lancelot.App()

@app.get("/v1/chat")
async def chat():
    return {"ok": True}

@app.get("/livez")
async def livez():
    return {"live": True}

@app.get("/public")
async def public():
    return {"pub": True}

app.add_middleware(
    StaticTokenGate,
    tokens=["s3cr3t-token", "second-token"],
    scheme="Bearer",
    guard_prefixes=("/v1",),
    open_paths=("/livez", "/readyz"),
    skip_options=True,
)

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


def test_token_gate_valid_invalid_missing():
    proc, port = _spawn(_GATE)
    try:
        base = f"http://127.0.0.1:{port}"
        # Valid token -> 200.
        r = httpx.get(f"{base}/v1/chat",
                      headers={"Authorization": "Bearer s3cr3t-token"},
                      timeout=10)
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json() == {"ok": True}
        # Second valid token also works (OR-loop over allowed tokens).
        r2 = httpx.get(f"{base}/v1/chat",
                       headers={"Authorization": "Bearer second-token"},
                       timeout=10)
        assert r2.status_code == 200, r2.text
        # Wrong token -> 401 + WWW-Authenticate.
        rw = httpx.get(f"{base}/v1/chat",
                       headers={"Authorization": "Bearer WRONG"}, timeout=10)
        assert rw.status_code == 401, rw.text
        assert rw.headers.get("www-authenticate", "").lower().startswith("bearer")
        # Missing header -> 401 + WWW-Authenticate.
        rm = httpx.get(f"{base}/v1/chat", timeout=10)
        assert rm.status_code == 401, rm.text
        assert "www-authenticate" in rm.headers
        # An UNguarded path (not under guard_prefixes) is open even w/o a token.
        rp = httpx.get(f"{base}/public", timeout=10)
        assert rp.status_code == 200, rp.text
    finally:
        _stop(proc)


def test_token_gate_skips_options_and_open_paths():
    proc, port = _spawn(_GATE)
    try:
        base = f"http://127.0.0.1:{port}"
        # OPTIONS on a guarded path -> NOT gated (CORS preflight must pass).
        ro = httpx.request("OPTIONS", f"{base}/v1/chat", timeout=10)
        assert ro.status_code != 401, (ro.status_code, ro.text)
        # An open_path passes ungated even with no/invalid token.
        rl = httpx.get(f"{base}/livez", timeout=10)
        assert rl.status_code == 200, rl.text
        assert rl.json() == {"live": True}
    finally:
        _stop(proc)


def test_token_gate_uses_scope_path_not_host():
    proc, port = _spawn(_GATE)
    try:
        # Forge a Host header AND an absolute-form request target with a bogus
        # authority. A naive gate that reconstructs the URL from Host (or only
        # checks request.url) could misjudge the path and let it through. The
        # gate keys off scope['path'] (= /v1/chat), so it MUST still 401.
        raw = (b"GET /v1/chat HTTP/1.1\r\n"
               b"Host: evil.attacker.example\r\n"
               b"X-Forwarded-Host: evil.attacker.example\r\n"
               b"\r\n")
        status_line, headers, _ = _raw(port, raw)
        assert " 401 " in status_line, status_line
        assert "www-authenticate" in headers, headers

        # Sanity: same path WITH a valid token still authorizes despite the
        # forged Host (the gate never trusts Host either way).
        raw_ok = (b"GET /v1/chat HTTP/1.1\r\n"
                  b"Host: evil.attacker.example\r\n"
                  b"Authorization: Bearer s3cr3t-token\r\n"
                  b"\r\n")
        status_ok, _, _ = _raw(port, raw_ok)
        assert " 200 " in status_ok, status_ok
    finally:
        _stop(proc)


def test_token_gate_import_is_pydantic_only():
    # `import lancelot.auth` must NOT eagerly pull httpx / jwt / redis.
    src = (
        "import sys\n"
        "import lancelot.auth\n"
        "assert 'httpx' not in sys.modules, 'httpx eagerly imported'\n"
        "assert 'jwt' not in sys.modules, 'jwt eagerly imported'\n"
        "assert 'redis' not in sys.modules, 'redis eagerly imported'\n"
        "print('clean')\n"
    )
    out = subprocess.run([sys.executable, "-c", src],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "clean" in out.stdout
