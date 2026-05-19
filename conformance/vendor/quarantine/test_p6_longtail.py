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
     directory-index / HTML mode / multi-range. Single-range (incl. RFC
     suffix bytes=-N, malformed-400, unsatisfiable-416) + conditional +
     traversal-guard ship byte-faithfully in M1-P6.
Honesty notes (review reconciliation, NOT silently narrowed):
  * The plan's Task 5 promised asgi.py emits 400 "There was an error
    parsing the body" for an unparseable form body. That message is part
    of the multipart/form-data path (ledger item 2) — DEFERRED with it.
    M1-P6 ships urlencoded Form byte-faithfully; a malformed multipart
    body currently 422s (clean, no crash) instead of 400. The plan text
    is superseded by ledger item 2; the commit says "MUST-6 partial".
  * WS dependency-validation close: Lancelot emits ONLY the correct
    error(s) with close code 1008. Real FastAPI additionally emits a
    spurious {"loc":["query","websocket"],...} from mis-detecting the
    bare `websocket` param. Lancelot's close code (1008) and the genuine
    errors match; the reason string is intentionally SANER (no spurious
    artifact) — a deliberate, documented deviation, not a parity claim.
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
