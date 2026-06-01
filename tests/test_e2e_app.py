"""END-TO-END cohesion test for Lancelot (cardinal rule #2: REAL load, not just
unit tests). A representative AI-startup / LLM-proxy app (`tests/_e2e_app.py`)
wiring together many features at once is driven over REAL HTTP in a SUBPROCESS —
NOT TestClient — on BOTH serving surfaces:

  Surface 1 (uvicorn, full ASGI stack): `uvicorn tests._e2e_app:app`. Proves the
  features that resolve under the full stack: Pydantic-body validation (200 /
  422), a `Depends` dependency, the M17 `StaticTokenGate` (401 -> 200) +
  `RateLimit` (429 flood), an SSE stream with a terminal `[DONE]`, a RFC-9457
  `ProblemDetail`, `PageParams` DoS cap (422 oversize), the M18 `GZipMiddleware`
  (content-encoding: gzip), `/livez`, the lifespan-set readiness state visible to
  the first request, the lifetime-task heartbeat advancing on the server loop,
  and a clean SIGTERM (lifespan exit + heartbeat stop, no orphaned-task warning).

  Surface 2 (native `app.run()`): `python tests/_e2e_app.py`. Proves the native
  server cohesion: async handlers return 200 (reading the body via
  `request.json()` on this DI-less path), `/livez` served from the prebaked const
  tier, the lifespan ran (app.state visible to the first request — no
  AttributeError), the lifetime-task heartbeat advanced (loop-identity), the SSE
  stream framed + terminated, and a clean SIGTERM drain.

The harness mirrors `tests/test_m18_native_lifespan.py` /
`tests/test_m16_relay_cancel.py`.
"""
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

import pytest

httpx = pytest.importorskip("httpx")

_APP_PATH = os.path.join(os.path.dirname(__file__), "_e2e_app.py")
_REPO_ROOT = os.path.dirname(os.path.dirname(__file__))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_listen(proc, port, deadline_s=15):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited rc={proc.returncode}: "
                f"{proc.stderr.read().decode()[:1500]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not start listening in time")


def _readlines(path):
    try:
        with open(path) as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        return []


def _heartbeat_advanced(statefile, by=2, timeout=4.0):
    """Poll until the HEARTBEAT count grows by `by` (the lifetime daemon is
    alive on the SERVER's loop — loop-identity). Returns (first, later)."""
    first = _readlines(statefile).count("HEARTBEAT")
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = _readlines(statefile).count("HEARTBEAT")
        if cur >= first + by:
            return first, cur
        time.sleep(0.05)
    return first, _readlines(statefile).count("HEARTBEAT")


def _stop(proc, sig=signal.SIGTERM, timeout=12):
    proc.send_signal(sig)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return None


# =============================================================================
# Surface 1 — uvicorn (full ASGI stack): DI + body validation + middleware
# =============================================================================
def _spawn_uvicorn(statefile, drain_secs="5"):
    port = _free_port()
    env = {**os.environ,
           "LANCELOT_E2E_STATEFILE": statefile,
           "LANCELOT_DRAIN_SECS": drain_secs}
    # Run uvicorn from the repo root so `tests._e2e_app:app` imports.
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests._e2e_app:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning", "--no-access-log",
         "--loop", "uvloop", "--http", "httptools"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=_REPO_ROOT)
    _wait_listen(proc, port)
    return proc, port


_AUTH = {"Authorization": "Bearer e2e-secret-token"}


def test_uvicorn_full_stack_cohesion():
    statefile = tempfile.mktemp(suffix=".e2e.log")
    proc, port = _spawn_uvicorn(statefile)
    base = f"http://127.0.0.1:{port}"
    try:
        # ---- lifespan startup visible to the FIRST request --------------------
        # /readyz-app reads app.state.ready that the lifespan CM set on enter.
        # An unset state would AttributeError -> 500.
        r = httpx.get(f"{base}/readyz-app", timeout=10)
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json() == {"ready": True, "boot_id": "BOOT-OK"}, r.json()

        # ---- the const-equivalent /livez (200 on this surface too) -----------
        rl = httpx.get(f"{base}/livez", timeout=10)
        assert rl.status_code == 200 and rl.json() == {"status": "ok"}, rl.text

        # ---- StaticTokenGate: guarded /v1 path -> 401 w/o token, 200 with -----
        no_tok = httpx.post(f"{base}/v1/chat", json={"prompt": "hi"}, timeout=10)
        assert no_tok.status_code == 401, no_tok.text
        assert "www-authenticate" in no_tok.headers

        # ---- Pydantic body + Depends: valid body -> 200, dep resolved ---------
        ok = httpx.post(f"{base}/v1/chat", json={"prompt": "hello", "max_tokens": 7},
                        headers={**_AUTH, "x-tenant-id": "acme"}, timeout=10)
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["completion"] == "you said: hello", body
        assert body["tenant"] == "acme", body          # Depends(current_tenant)
        assert body["max_tokens"] == 7, body

        # ---- malformed body -> 422 (full-stack validation) -------------------
        bad = httpx.post(f"{base}/v1/chat", json={"nope": 1},
                         headers=_AUTH, timeout=10)
        assert bad.status_code == 422, bad.text
        assert bad.json()["detail"][0]["loc"] == ["body", "prompt"], bad.json()

        # ---- ProblemDetail (RFC 9457) -> application/problem+json + fields ----
        prob = httpx.get(f"{base}/v1/boom", headers=_AUTH, timeout=10)
        assert prob.status_code == 402, prob.text
        assert prob.headers["content-type"].startswith(
            "application/problem+json"), prob.headers["content-type"]
        pj = prob.json()
        assert pj["type"] == "https://errors.example/quota"
        assert pj["title"] == "Quota Exceeded"
        assert pj["status"] == 402
        assert pj["detail"] == "monthly token budget exhausted"
        assert pj["instance"] == "/v1/boom"
        assert pj["balance_usd"] == 0.0               # extension member merged

        # ---- PageParams: in-range OK; oversize -> 422 ------------------------
        pg = httpx.get(f"{base}/v1/items?page=2&size=10", headers=_AUTH, timeout=10)
        assert pg.status_code == 200, pg.text
        pgj = pg.json()
        assert pgj["page"] == 2 and pgj["size"] == 10
        assert pgj["items"][0]["id"] == 10 and len(pgj["items"]) == 10
        over = httpx.get(f"{base}/v1/items?size=9999", headers=_AUTH, timeout=10)
        assert over.status_code == 422, over.text

        # ---- GZipMiddleware: large body + Accept-Encoding gzip -> compressed --
        # The items list is well over the 500-byte min when size is large.
        gz = httpx.get(f"{base}/v1/items?size=50",
                       headers={**_AUTH, "Accept-Encoding": "gzip"},
                       timeout=10)
        assert gz.status_code == 200, gz.text
        assert gz.headers.get("content-encoding") == "gzip", dict(gz.headers)
        assert "accept-encoding" in gz.headers.get("vary", "").lower()
        # httpx transparently decoded it; the rows are intact.
        assert len(gz.json()["items"]) == 50

        # ---- SSE stream: framed chunks + terminal data: [DONE] ---------------
        with httpx.stream("GET", f"{base}/v1/stream", headers=_AUTH,
                          timeout=10) as s:
            assert s.status_code == 200, s.status_code
            assert s.headers["content-type"].startswith("text/event-stream")
            text = "".join(s.iter_text())
        assert "tok0" in text and "tok3" in text, text
        assert '"index":99' in text or '"text":"fin"' in text, text
        assert "data: [DONE]" in text, text           # M16/M17 terminal sentinel

        # ---- the lifetime daemon keeps ticking on the server loop ------------
        first, later = _heartbeat_advanced(statefile, by=2)
        assert later >= first + 2, (
            f"lifetime heartbeat did not advance on the server loop: "
            f"{first} -> {later}; lines={_readlines(statefile)}")

        # ---- RateLimit: a flood trips 429 (burst=100 / window) ---------------
        # /livez is an open_path for the gate AND is rate-limited per IP; hammer
        # it well past the burst to force a shed. (Use /livez so the gate doesn't
        # 401 before the limiter is reached.) The 429 also carries Retry-After +
        # RateLimit-* headers — assert the shed shape is well-formed.
        shed = None
        for _ in range(150):
            resp = httpx.get(f"{base}/livez", timeout=10)
            if resp.status_code == 429:
                shed = resp
                break
        assert shed is not None, "RateLimit never shed under a 150-request flood"
        assert "retry-after" in shed.headers, dict(shed.headers)
        assert shed.headers.get("ratelimit-remaining") == "0", dict(shed.headers)

        # ---- lifespan markers present (root + mounted sub-app, LIFO order) ----
        lines = _readlines(statefile)
        assert "ROOT_ENTER" in lines, lines
        assert "SUB_ENTER" in lines, lines
        assert lines.index("ROOT_ENTER") < lines.index("SUB_ENTER"), lines

        # ---- SIGTERM -> clean shutdown: lifespan exit + heartbeat stop --------
        rc = _stop(proc)
        err = proc.stderr.read().decode()
        lines = _readlines(statefile)
        assert "ROOT_EXIT" in lines, lines
        assert "SUB_EXIT" in lines, lines
        assert "HEARTBEAT_FINALLY" in lines, lines
        fin = lines.index("HEARTBEAT_FINALLY")
        assert "HEARTBEAT" not in lines[fin:], lines[fin:]
        assert "Task was destroyed but it is pending" not in err, err
        # lifespan drove exactly once.
        assert lines.count("ROOT_ENTER") == 1 and lines.count("ROOT_EXIT") == 1, lines
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            os.remove(statefile)
        except OSError:
            pass


# =============================================================================
# Surface 2 — native `app.run()` (Rust server): async handlers + lifespan + const
# =============================================================================
def _spawn_native(statefile, drain_secs="5"):
    port = _free_port()
    env = {**os.environ,
           "LANCELOT_E2E_STATEFILE": statefile,
           "LANCELOT_HOST": "127.0.0.1",
           "LANCELOT_DRAIN_SECS": drain_secs}
    proc = subprocess.Popen(
        [sys.executable, _APP_PATH, str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=_REPO_ROOT)
    _wait_listen(proc, port)
    return proc, port


def test_native_server_cohesion():
    statefile = tempfile.mktemp(suffix=".e2e.log")
    proc, port = _spawn_native(statefile)
    base = f"http://127.0.0.1:{port}"
    try:
        # ---- lifespan startup visible to the FIRST request (no AttributeError)
        # The native server shares the same `app` object the lifespan mutated.
        r = httpx.get(f"{base}/readyz-app", timeout=10)
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json() == {"ready": True, "boot_id": "BOOT-OK"}, r.json()

        # ---- /livez served from the prebaked const tier ----------------------
        # (const_route overrides the handler route on the native surface.)
        rl = httpx.get(f"{base}/livez", timeout=10)
        assert rl.status_code == 200, rl.text
        assert rl.json() == {"status": "ok"}, rl.text
        assert rl.headers.get("content-type", "").startswith("application/json")

        # ---- an async handler returns 200 reading the body itself (no DI) -----
        ok = httpx.post(f"{base}/v1/chat",
                        json={"prompt": "native", "max_tokens": 3}, timeout=10)
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["completion"] == "you said: native", body
        assert body["max_tokens"] == 3, body
        # No auth gate on the native surface (middleware is uvicorn-only) — the
        # request was NOT 401'd, confirming the documented surface difference.

        # ---- pagination resolves on the native path (handler self-resolves) ---
        pg = httpx.get(f"{base}/v1/items?page=1&size=5", timeout=10)
        assert pg.status_code == 200, pg.text
        assert len(pg.json()["items"]) == 5, pg.json()

        # ---- SSE stream framed + terminated on the native R3 pump -------------
        with httpx.stream("GET", f"{base}/v1/stream", timeout=10) as s:
            assert s.status_code == 200, s.status_code
            assert s.headers["content-type"].startswith("text/event-stream")
            text = "".join(s.iter_text())
        assert "tok0" in text and "tok3" in text, text
        assert "data: [DONE]" in text, text

        # ---- the mounted sub-app's lifespan ran (loop-identity) --------------
        lines = _readlines(statefile)
        assert "ROOT_ENTER" in lines, lines
        assert "SUB_ENTER" in lines, lines

        # ---- the lifetime daemon advanced on the server's own loop -----------
        first, later = _heartbeat_advanced(statefile, by=2)
        assert later >= first + 2, (
            f"native lifetime heartbeat did not advance: {first} -> {later}; "
            f"lines={_readlines(statefile)}")

        # ---- SIGTERM -> lifespan shutdown + heartbeat stop within drain -------
        rc = _stop(proc)
        assert rc is not None, "native server did not exit on SIGTERM (had to kill)"
        err = proc.stderr.read().decode()
        lines = _readlines(statefile)
        assert "ROOT_EXIT" in lines, lines
        assert "SUB_EXIT" in lines, lines
        assert "HEARTBEAT_FINALLY" in lines, lines
        fin = lines.index("HEARTBEAT_FINALLY")
        assert "HEARTBEAT" not in lines[fin:], lines[fin:]
        assert "Task was destroyed but it is pending" not in err, err
        assert lines.count("ROOT_ENTER") == 1 and lines.count("ROOT_EXIT") == 1, lines
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            os.remove(statefile)
        except OSError:
            pass
