"""M1-P6 LEDGER (quarantined, ratchet-excluded). M1-P6 ships WS (echo
text+binary, /ws/{id} param, DI, validation→close 1008) + StaticFiles
(ETag/Last-Modified/single-Range/304/traversal-guard, byte-faithful to
starlette 1.0.0) + Settings (pydantic-settings DI provider, no runtime
dep) + urlencoded Form (byte-faithful to fastapi 0.136.1) on the Python
ASGI seam (the path the oracle exercises). RED = out of M1-P6 scope:
  1. Rust src/ws/mod.rs (tokio-tungstenite, bounded mpsc backpressure)
     + src/static_files.rs (tower-http::fs, sendfile(2) zero-copy) wired
     into the Rust hyper path — spec §3-69 unification. The bounded-
     channel back-pressure of WBS U13's done-when is the Rust mpsc.
  2. multipart/form-data + UploadFile + the 400 "There was an error
     parsing the body" message — real FastAPI gates ALL Form (urlencoded
     included) behind python-multipart; M1-P6 ships the urlencoded path
     with stdlib (no runtime dep) and python-multipart is a TEST-only
     oracle dep. multipart/file streaming is the remaining tail.
  3. WS subprotocol negotiation + permessage-deflate; static
     directory-index / HTML mode / multi-range. Single-range +
     conditional + traversal-guard ship in M1-P6.
Graduate into conformance/vendor/ as M3 + the §3-69 unification land."""
import pytest


def test_rust_ws_and_static_unification_S369():
    pytest.fail("spec §3-69: Rust src/ws/mod.rs + src/static_files.rs "
                "wired into the Rust hyper path (M1-P6 ships the "
                "byte-faithful Python-ASGI-seam realization)")


def test_multipart_form_data_and_upload_file():
    pytest.fail("multipart/form-data + UploadFile + 400 'error parsing "
                "the body' — urlencoded Form ships in M1-P6 (stdlib)")


def test_ws_subprotocol_static_multirange_dirindex():
    pytest.fail("WS subprotocol/permessage-deflate; static "
                "directory-index/HTML/multi-range (§3-69 / M3)")
