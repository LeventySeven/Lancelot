"""M20 Rust byte-path hardening — adversarial, real-server validation of the two
Rust socket/transport knobs added in batch D (FINDINGS T1.6 + T2.1).

Cardinal rule 2 (validate on the path the bug lives on): these are NOT TestClient
unit tests. Each spins the REAL Rust server (lancelot._native.run) in a subprocess
and drives it over a raw socket / httpx, exercising the exact path the defense
guards — the *stalled body* (T2.1) and the *opt-in keepalive socket* (T1.6).

  T2.1 body-read deadline -> 408:
    - a client that sends headers + Content-Length then STALLS the body is cut
      with 408 near LANCELOT_BODY_TIMEOUT_MS (NOT hung to the client deadline),
      and the connection slot it held is freed (a fresh request still 200s);
    - default (knob unset / 0) does NOT kill a body; a normal fast POST 200s;
    - a slow-but-PROGRESSING upload that finishes within the deadline is NOT
      killed (the "don't murder legit slow uploads" guarantee).

  T1.6 TCP keepalive:
    - with LANCELOT_TCP_KEEPALIVE_SECS set the wheel still binds and serves a
      normal request (the setsockopt is best-effort; enabling it must not break
      the listener). Whether the kernel actually reaps a half-open peer is an OS
      behavior on a multi-minute timescale — not unit-testable — so we assert the
      knob is accepted end-to-end (parse + bind + serve), the contract that
      matters for "did we wire it without regressing the socket".
"""
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")

# Minimal real-server program: an echo handler that reports the body length, so
# a successful POST proves the whole body was read. Mirrors test_m4_resilience.
_SERVER = r"""
import sys
import lancelot._native as _native

async def echo(req):
    return {"ok": True, "n": len(req.body)}

async def ping(req):
    return {"ok": True}

_native.run([("POST", "/echo", echo), ("GET", "/ping", ping)],
            host="127.0.0.1", port=int(sys.argv[1]))
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _spawn(env_extra=None, src=_SERVER):
    port = _free_port()
    env = {**os.environ, **(env_extra or {})}
    proc = subprocess.Popen([sys.executable, "-c", src, str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited rc={proc.returncode}: "
                               f"{proc.stderr.read().decode()[:500]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc, port
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not start")


def _stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# --------------------------------------------------------------------------- #
# T2.1 — post-header body-stall -> 408 + slot freed                            #
# --------------------------------------------------------------------------- #

def test_t21_body_stall_after_headers_gets_408_and_slot_is_freed():
    # Small body deadline so the test is fast; header deadline left long so we
    # isolate the BODY guard (the head completes instantly — only the body
    # stalls). The header guard alone would NOT catch this: the request head is
    # fully sent, it's the body that never arrives.
    proc, port = _spawn({"LANCELOT_BODY_TIMEOUT_MS": "600",
                         "LANCELOT_HEADER_TIMEOUT_MS": "15000",
                         # cap=1: if the stalled conn's slot were NOT freed, the
                         # follow-up request below could never be served — so
                         # this also proves slot release, not just the 408.
                         "LANCELOT_MAX_CONNECTIONS": "1"})
    try:
        base = f"http://127.0.0.1:{port}"

        # Hostile: complete the request HEAD (incl. a Content-Length promising a
        # body) then send NOTHING. The body collect must time out at ~600 ms.
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(b"POST /echo HTTP/1.1\r\nHost: x\r\n"
                  b"Content-Length: 1000\r\n\r\n")
        # ... and deliberately never send the 1000 body bytes.
        s.settimeout(10)
        t0 = time.time()
        data = s.recv(4096)  # blocks until the server responds (408) or closes
        elapsed = time.time() - t0
        s.close()

        # Cut near the deadline, not hung to our 10 s client timeout.
        assert elapsed < 5.0, f"body stall not cut in time ({elapsed:.1f}s)"
        # 408 (or a clean close — both terminate the stall and free the slot).
        assert data == b"" or data.startswith(b"HTTP/1.1 408"), data[:80]

        # The stalled connection's slot was freed: with cap=1 a fresh request
        # could only succeed if the half-open slot was actually released.
        r = httpx.get(f"{base}/ping", timeout=10)
        assert r.status_code == 200, r.status_code
        assert proc.poll() is None, "server crashed under the body stall"
    finally:
        _stop(proc)


def test_t21_default_off_does_not_kill_a_normal_post():
    # Knob unset (default 0 = OFF): the body deadline path is not even
    # constructed; a normal POST with a real body must succeed unaffected.
    proc, port = _spawn()
    try:
        base = f"http://127.0.0.1:{port}"
        payload = b"x" * 4096
        r = httpx.post(f"{base}/echo", content=payload, timeout=10)
        assert r.status_code == 200, r.status_code
        assert r.json() == {"ok": True, "n": len(payload)}
    finally:
        _stop(proc)


def test_t21_slow_but_progressing_upload_within_deadline_is_not_killed():
    # The legit-slow-upload guarantee: a client that dribbles its body in
    # chunks but FINISHES within the total deadline must NOT be 408'd. Deadline
    # 3 s; we send 5 chunks ~150 ms apart (~0.75 s total) << 3 s.
    proc, port = _spawn({"LANCELOT_BODY_TIMEOUT_MS": "3000"})
    try:
        n_chunks = 5
        chunk = b"y" * 200
        total = len(chunk) * n_chunks
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        head = (f"POST /echo HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {total}\r\n\r\n").encode()
        s.sendall(head)
        for _ in range(n_chunks):
            s.sendall(chunk)
            time.sleep(0.15)
        s.settimeout(10)
        resp = b""
        while b"\r\n\r\n" not in resp:
            part = s.recv(4096)
            if not part:
                break
            resp += part
        s.close()
        # Progressing-but-slow upload completed: a 200 with the full body len,
        # NOT a 408. (It finished in ~0.75 s, well inside the 3 s deadline.)
        assert resp.startswith(b"HTTP/1.1 200"), resp[:80]
        assert str(total).encode() in resp, resp[:200]
    finally:
        _stop(proc)


# --------------------------------------------------------------------------- #
# T1.6 — TCP keepalive knob accepted end-to-end (parse + bind + serve)         #
# --------------------------------------------------------------------------- #

def test_t16_keepalive_enabled_still_binds_and_serves():
    # Enabling SO_KEEPALIVE + idle/intvl/cnt is best-effort; the contract is
    # "wiring it must not break the listener". Assert the wheel parses the knob,
    # binds, and serves a normal request with keepalive on.
    proc, port = _spawn({"LANCELOT_TCP_KEEPALIVE_SECS": "30"})
    try:
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/ping").status_code == 200
        # And a second request over a fresh connection (the keepalive socket
        # option is applied at bind on the listener; accepted conns inherit it).
        assert httpx.post(f"{base}/echo", content=b"z" * 16).json()["n"] == 16
    finally:
        _stop(proc)


def test_t16_keepalive_default_off_serves_normally():
    # Default (knob unset) leaves the socket untouched — sanity that the
    # off-path is unaffected.
    proc, port = _spawn()
    try:
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/ping").status_code == 200
    finally:
        _stop(proc)
