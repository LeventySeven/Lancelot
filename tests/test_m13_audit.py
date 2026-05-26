"""M13 — security + bug fixes from the from-scratch multi-agent audit.

  SEC1 — CORSMiddleware must reject allow_credentials=True + wildcard origin
         (Starlette parity: that combo reflects any Origin with credentials =
         SOP bypass).
  SEC2 — FileBackend.get() must not escape its base dir (path traversal).
  SEC3 — SSE Event id/name must not carry CR/LF/NUL (frame injection +
         lastEventId reset).
  BUG1 — multipart over-limit (the M8 DoS caps) must surface as 400, not 500.
"""
import os
import tempfile

import pytest

import lancelot
from lancelot.testclient import TestClient


# ---------- SEC1: CORS credentials + wildcard ---------------------------

def test_sec1_cors_credentials_plus_wildcard_rejected():
    from lancelot.middleware import CORSMiddleware

    async def _app(scope, receive, send):  # noqa: ARG001
        pass

    with pytest.raises(ValueError, match="(?i)credential"):
        CORSMiddleware(_app, allow_origins=["*"], allow_credentials=True)


def test_sec1_cors_wildcard_without_credentials_ok():
    from lancelot.middleware import CORSMiddleware

    async def _app(scope, receive, send):  # noqa: ARG001
        pass

    # The safe combos still construct fine.
    CORSMiddleware(_app, allow_origins=["*"])
    CORSMiddleware(_app, allow_origins=["https://x.com"], allow_credentials=True)


# ---------- SEC2: FileBackend path traversal ----------------------------

def test_sec2_filebackend_no_path_traversal():
    from lancelot.secrets import FileBackend

    d = tempfile.mkdtemp()
    with open(os.path.join(d, "db_password"), "w") as f:
        f.write("s3cret\n")
    b = FileBackend(d)
    assert b.get("db_password") == "s3cret"          # normal lookup works
    assert b.get("../../etc/passwd") is None          # relative escape blocked
    assert b.get("/etc/passwd") is None               # absolute escape blocked


# ---------- SEC3: SSE Event id/name CR/LF/NUL --------------------------

def test_sec3_sse_event_id_strips_control_chars():
    from lancelot.stream import Event
    out = Event("delta", {"x": 1}, id="a\0b\nc\rd").render()
    assert b"\x00" not in out
    assert b"id: abcd" in out, out          # control chars stripped, id intact
    # exactly one id line (no extra frames smuggled via the newline)
    assert out.count(b"id:") == 1


def test_sec3_sse_event_name_strips_newline():
    from lancelot.stream import Event
    out = Event("del\nta", {"x": 1}).render()
    assert b"event: delta" in out
    assert out.count(b"event:") == 1


# ---------- BUG1: multipart over-limit → 400 (not 500) ------------------

def test_bug1_multipart_over_limit_is_400_not_500():
    from lancelot.params import Form

    app = lancelot.App()

    @app.post("/up")
    def up(x: str = Form()):
        return {"x": x}

    boundary = "BOUND"
    parts = []
    for i in range(1100):  # > the 1000-field default cap
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="f{i}"\r\n\r\nv\r\n')
    parts.append(f"--{boundary}--\r\n")
    body = "".join(parts).encode()

    with TestClient(app) as client:
        r = client.post("/up", content=body,
                        headers={"content-type":
                                 f"multipart/form-data; boundary={boundary}"})
    # The DoS cap fires — but it's a client error (bad request), not a 500.
    assert r.status_code == 400, r.status_code
