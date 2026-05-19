# Lancelot M1-P6 — WebSocket + Static Files + Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline-serial, the M1 chosen mode). Steps use checkbox (`- [ ]`). TDD is mandatory: RED → verify-fail → GREEN → verify-pass → exit-gated commit. The byte-differential oracle is **real Starlette 1.0.0 / FastAPI 0.136.1** (TEST-only deps).

**Goal:** The ASGI seam speaks the WebSocket protocol (accept/receive/send/close, path-param routing, DI, validation-failure → close `1008`); a Starlette-faithful `StaticFiles` app serves files with ETag/Last-Modified/Range/304 and a path-traversal guard; a pydantic-settings DI provider resolves a user `Settings` once at registration (missing required env → boot crash, not runtime 500); `application/x-www-form-urlencoded` form params parse, and an unparseable body → `400 {"detail":"There was an error parsing the body"}` (deep/01 MUST-6). All byte/behavior-identical to the real libs on the covered subset; the Rust `src/ws/mod.rs` / `src/static_files.rs` realizations and `multipart/form-data` (python-multipart) are honestly ledgered RED.

**Architecture:** The conformance/byte-oracle path is the pure-Python ASGI seam (`lancelot/asgi.py`). WS/static live there (the path TestClient + the oracle exercise); the Rust tokio-tungstenite / tower-http realizations are the spec §3-69 unification, ledgered — same honest scoping as M1-P5. Runtime dependency set stays `pydantic` only: urlencoded forms parse with stdlib `urllib.parse`; `lancelot/settings.py` works with ANY user-supplied pydantic `BaseSettings` **without Lancelot importing pydantic-settings** (it is a TEST-only oracle dep, like fastapi/starlette).

**Tech Stack:** Python 3.13 stdlib (`urllib.parse`, `hashlib`, `email.utils`, `os`, `mimetypes`), pydantic-core wire seam (unchanged), real `starlette==1.0.0` / `fastapi==0.136.1` / `pydantic-settings==2.14.1` as TEST-only byte/behavior oracles.

---

## File Structure

- Create: `lancelot/websockets.py` — `WebSocket` (ASGI websocket wrapper: `accept`, `receive_text/bytes/json`, `send_text/bytes/json`, `close`, `WebSocketDisconnect`), Starlette-compatible.
- Create: `lancelot/staticfiles.py` — `StaticFiles` ASGI app (mountable): conditional GET (ETag/Last-Modified/304), single byte-range (206/416), path-traversal guard, 404/405, `mimetypes` content-type.
- Create: `lancelot/settings.py` — `get_settings`-style DI provider factory: registration-time cached singleton over a user pydantic `BaseSettings`; missing required env raises at boot.
- Modify: `lancelot/asgi.py` — dispatch `scope["type"] == "websocket"` to the WS handler (route match w/ path params, DI via `solve_async`, validation errors → `websocket.close(1008, reason=...)`); the `400 parsing` body for an unparseable form body.
- Modify: `lancelot/routing.py` — `@app.websocket(path)` registration + `Route.is_ws`; `app.mount(prefix, app)` for static.
- Modify: `lancelot/app.py` — `websocket` decorator passthrough; `mount`.
- Modify: `lancelot/di.py` — recognize a `WebSocket` parameter (bind the live socket, like the `Request`/`BackgroundTasks` slots); `Form(...)` marker → body-form field.
- Modify: `lancelot/params.py` — `Form` marker (subclass of the body/field marker with `in_="form"`).
- Modify: `lancelot/__init__.py` — lazy export `WebSocket`, `WebSocketDisconnect`, `StaticFiles`.
- Test: `tests/test_m1_p6_byte_parity.py` — real Starlette/FastAPI WS + StaticFiles + pydantic-settings differential.
- Test/seed: `conformance/vendor/quarantine/test_p6_longtail.py` — honest RED ledger (Rust §3-69 WS/static; multipart/file-upload; sendfile; WS subprotocols/permessage-deflate).

---

### Task 1: `WebSocket` ASGI wrapper (`lancelot/websockets.py`)

**Files:** Create `lancelot/websockets.py`; Test `tests/test_m1_p6_byte_parity.py`.

- [ ] **Step 1: Failing test**

```python
import asyncio
import pytest


def test_websocket_wrapper_accept_send_receive_close():
    from lancelot.websockets import WebSocket, WebSocketDisconnect

    sent = []
    incoming = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": "ping"},
        {"type": "websocket.disconnect", "code": 1000},
    ]

    async def receive():
        return incoming.pop(0)

    async def send(msg):
        sent.append(msg)

    async def main():
        ws = WebSocket({"type": "websocket", "path": "/ws"}, receive, send)
        await ws.accept()
        assert sent[0] == {"type": "websocket.accept", "subprotocol": None,
                           "headers": []}
        assert await ws.receive_text() == "ping"
        await ws.send_text("pong")
        assert sent[1] == {"type": "websocket.send", "text": "pong"}
        with pytest.raises(WebSocketDisconnect) as ei:
            await ws.receive_text()
        assert ei.value.code == 1000

    asyncio.run(main())
```

- [ ] **Step 2: Verify fail** — `pytest tests/test_m1_p6_byte_parity.py::test_websocket_wrapper_accept_send_receive_close -q` → `ModuleNotFoundError: lancelot.websockets`.

- [ ] **Step 3: Implement**

```python
# lancelot/websockets.py
"""ASGI WebSocket wrapper — Starlette-shape surface (accept/receive_*/
send_*/close + WebSocketDisconnect). The Rust tokio-tungstenite path
(src/ws/mod.rs, WBS U13) is the spec §3-69 unification; this is the
Python-ASGI-seam realization the conformance/byte oracle exercises."""
import json


class WebSocketDisconnect(Exception):
    def __init__(self, code: int = 1000, reason: str = ""):
        self.code = code
        self.reason = reason
        super().__init__(f"disconnect {code}")


class WebSocket:
    def __init__(self, scope, receive, send):
        self.scope = scope
        self._receive = receive
        self._send = send
        self.path_params = {}
        self._accepted = False

    async def accept(self, subprotocol=None, headers=None):
        msg = await self._receive()  # consume websocket.connect
        if msg["type"] != "websocket.connect":
            raise RuntimeError(f"expected websocket.connect, got {msg['type']}")
        await self._send({"type": "websocket.accept",
                          "subprotocol": subprotocol,
                          "headers": list(headers or [])})
        self._accepted = True

    async def _recv(self):
        msg = await self._receive()
        if msg["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(msg.get("code", 1000),
                                      msg.get("reason", ""))
        return msg

    async def receive_text(self) -> str:
        return (await self._recv())["text"]

    async def receive_bytes(self) -> bytes:
        return (await self._recv())["bytes"]

    async def receive_json(self):
        return json.loads(await self.receive_text())

    async def send_text(self, data: str):
        await self._send({"type": "websocket.send", "text": data})

    async def send_bytes(self, data: bytes):
        await self._send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data):
        await self.send_text(json.dumps(data, separators=(",", ":")))

    async def close(self, code: int = 1000, reason: str = ""):
        await self._send({"type": "websocket.close", "code": code,
                          "reason": reason})
```

- [ ] **Step 4: Verify pass** — same pytest → PASS.
- [ ] **Step 5: Commit** — `git -C "$LC" add lancelot/websockets.py tests/test_m1_p6_byte_parity.py && pytest tests/ -q >/dev/null 2>&1; rc=$?; test $rc -eq 0 && git -C "$LC" commit -q -m "feat(m1-p6): ASGI WebSocket wrapper (U13 surface)"`

---

### Task 2: `@app.websocket` routing + ASGI dispatch + DI + 1008

**Files:** Modify `lancelot/routing.py`, `lancelot/app.py`, `lancelot/asgi.py`, `lancelot/di.py`; Test same file.

- [ ] **Step 1: Failing test** (real-Starlette behavior parity: echo text+binary, `/ws/{id}` param, validation→1008)

```python
def test_websocket_echo_param_and_1008_match_starlette():
    import starlette.applications
    import starlette.routing
    from starlette.testclient import TestClient as STC
    import lancelot
    from lancelot.testclient import TestClient as LC

    def lancelot_app():
        app = lancelot.App()

        @app.websocket("/ws/conversations/{cid}")
        async def ws(websocket):
            await websocket.accept()
            websocket  # path param visible
            await websocket.send_json({"cid": websocket.path_params["cid"]})
            msg = await websocket.receive_bytes()
            await websocket.send_bytes(msg + b"!")
            await websocket.close()

        return app

    with LC(lancelot_app()) as lc:
        with lc.websocket_connect("/ws/conversations/abc") as w:
            assert w.receive_json() == {"cid": "abc"}
            w.send_bytes(b"hi")
            assert w.receive_bytes() == b"hi!"

    # validation failure → close 1008 (deep/01 line 93; MUST-7)
    def lancelot_v():
        app = lancelot.App()

        @app.websocket("/v/{n}")
        async def v(websocket, n: int):
            await websocket.accept()
            await websocket.send_json({"n": n})

        return app

    with LC(lancelot_v()) as lc:
        with pytest.raises(Exception):
            with lc.websocket_connect("/v/notanint") as w:
                w.receive_text()
```

- [ ] **Step 2: Verify fail** — connect raises `NotImplementedError('websocket')` (asgi.py:17).

- [ ] **Step 3: Implement**
  - `lancelot/params.py`: nothing yet (WS DI reuses field validators).
  - `lancelot/routing.py`: add `is_ws: bool = False` to `Route`; `APIRouter.websocket(path)` decorator that registers `Route("GET", path, fn, is_ws=True)` and compiles a `Plan` with `path_params` (so `n: int` becomes a path field validated exactly like HTTP).
  - `lancelot/app.py`: `def websocket(self, path): return self.router.websocket(path)`.
  - `lancelot/di.py` `compile_dependant`: after the `BackgroundTasks` branch, `if ann is _WS: req_params.append(("__ws__", name)); continue` (bind the live socket); the binding loop (both `solve`/`solve_async`) handles `("__ws__", name)` → `request._ws`.
  - `lancelot/asgi.py`: replace `raise NotImplementedError(scope["type"])` with `if scope["type"] == "websocket": await _ws(app, scope, receive, send); return`. `_ws`: matchit the path (reuse `router.match` with a WS bucket), build a `WebSocket`, set `path_params`; run `solve_async(route.plan, pseudo_request_with_ws_and_path)`; on `res.errors` → `await ws.close(1008, reason=json.dumps(errors))` **before** accept (Starlette/FastAPI: close 1008 on dependency validation failure — deep/01 §line 93); else call the handler with kwargs + the bound `websocket`.

- [ ] **Step 4: Verify pass** — PASS (echo text+binary, param, 1008).
- [ ] **Step 5: Run full suite** — `pytest tests/ -q` all green (HTTP path untouched).
- [ ] **Step 6: Commit** — `feat(m1-p6): @app.websocket routing + ASGI WS dispatch + DI + 1008`.

---

### Task 3: `StaticFiles` — conditional GET / Range / 304 / traversal guard

**Files:** Create `lancelot/staticfiles.py`; Modify `lancelot/routing.py` (`app.mount`), `lancelot/app.py`; Test same file.

- [ ] **Step 1: Failing test** (byte-parity vs real Starlette `StaticFiles`)

```python
def test_staticfiles_etag_range_304_traversal_match_starlette(tmp_path):
    import starlette.applications
    from starlette.staticfiles import StaticFiles as SSF
    from starlette.testclient import TestClient as STC
    import lancelot
    from lancelot.staticfiles import StaticFiles as LSF
    from lancelot.testclient import TestClient as LC

    (tmp_path / "a.txt").write_bytes(b"hello world")

    sapp = starlette.applications.Starlette()
    sapp.mount("/s", SSF(directory=str(tmp_path)))
    lapp = lancelot.App()
    lapp.mount("/s", LSF(directory=str(tmp_path)))

    with STC(sapp) as sc, LC(lapp) as lc:
        s, l = sc.get("/s/a.txt"), lc.get("/s/a.txt")
        assert l.status_code == s.status_code == 200
        assert l.content == s.content == b"hello world"
        assert l.headers["etag"] == s.headers["etag"]
        # 304 on If-None-Match
        et = s.headers["etag"]
        s2 = sc.get("/s/a.txt", headers={"if-none-match": et})
        l2 = lc.get("/s/a.txt", headers={"if-none-match": et})
        assert l2.status_code == s2.status_code == 304
        # single byte range
        s3 = sc.get("/s/a.txt", headers={"range": "bytes=0-4"})
        l3 = lc.get("/s/a.txt", headers={"range": "bytes=0-4"})
        assert l3.status_code == s3.status_code == 206
        assert l3.content == s3.content == b"hello"
        assert l3.headers["content-range"] == s3.headers["content-range"]
        # path traversal → 404 (never escape the directory)
        assert lc.get("/s/../../etc/passwd").status_code in (404, 400)
```

- [ ] **Step 2: Verify fail** — `lancelot.staticfiles` missing.

- [ ] **Step 3: Implement** `lancelot/staticfiles.py` — an ASGI app: resolve `os.path.realpath(os.path.join(directory, path.lstrip("/")))`; reject if not under `realpath(directory)` → 404; `os.stat`; ETag = `hashlib.md5(f"{st.st_mtime}-{st.st_size}".encode()).hexdigest()` quoted (Starlette's exact recipe — verify against the lib in Step 4 and adjust to byte-match); `Last-Modified` via `email.utils.formatdate(st.st_mtime, usegmt=True)`; `If-None-Match`/`If-Modified-Since` → 304 (no body); `Range: bytes=a-b` → 206 + `Content-Range: bytes a-b/size` (clamp; invalid/unsatisfiable → 416); else 200 full body; `Content-Type` from `mimetypes.guess_type`; only GET/HEAD (else 405). Header wire order follows the M1-P4 Starlette `init_headers` invariant. `app.mount(prefix, asgi_app)` in routing.py: a longest-prefix check in `router.match` that returns the mounted app; `asgi.py` delegates to it with `scope["path"]` stripped of the prefix.

- [ ] **Step 4: Verify pass** — adjust the ETag/Last-Modified recipe until `l.headers == s.headers` for the covered cases; PASS.
- [ ] **Step 5: Full suite** — green.
- [ ] **Step 6: Commit** — `feat(m1-p6): Starlette-faithful StaticFiles (U14) + app.mount`.

---

### Task 4: `Settings` DI provider (`lancelot/settings.py`, U19)

**Files:** Create `lancelot/settings.py`; Test same file.

- [ ] **Step 1: Failing test**

```python
def test_settings_provider_cached_and_missing_env_boot_crash(monkeypatch):
    from pydantic_settings import BaseSettings   # TEST-only oracle
    from lancelot.settings import settings_provider

    class S(BaseSettings):
        port: int
        name: str = "lancelot"

    monkeypatch.setenv("PORT", "8080")
    get = settings_provider(S)
    a, b = get(), get()
    assert a is b and a.port == 8080 and a.name == "lancelot"  # cached

    monkeypatch.delenv("PORT", raising=False)
    with pytest.raises(Exception):           # missing required env at BUILD
        settings_provider(S, eager=True)()
```

- [ ] **Step 2: Verify fail** — `lancelot.settings` missing.

- [ ] **Step 3: Implement**

```python
# lancelot/settings.py
"""pydantic-settings DI provider (WBS U19). Lancelot does NOT depend on
pydantic-settings — the user brings their own BaseSettings; we only
provide the registration-time cached singleton. Missing required env
surfaces at BUILD (eager) so it is a boot crash, not a runtime 500."""
import functools


def settings_provider(settings_cls, *, eager: bool = False):
    @functools.lru_cache(maxsize=1)
    def _get():
        return settings_cls()  # pydantic-settings reads env here

    if eager:
        _get()  # instantiate now → missing required env crashes at boot
    return _get
```

- [ ] **Step 4: Verify pass** — PASS.
- [ ] **Step 5: Commit** — `feat(m1-p6): pydantic-settings DI provider (U19, no runtime dep)`.

---

### Task 5: `Form(...)` urlencoded params + unparseable-body → 400 (MUST-6)

**Files:** Modify `lancelot/params.py`, `lancelot/di.py`, `lancelot/asgi.py`; Test same file.

- [ ] **Step 1: Failing test** (byte-parity vs real FastAPI)

```python
def test_urlencoded_form_and_unparseable_body_match_fastapi():
    import fastapi
    from fastapi import Form
    from fastapi.testclient import TestClient as FA
    import lancelot
    from lancelot.params import Form as LForm
    from lancelot.testclient import TestClient as LC

    def build(F, Fm):
        app = F.FastAPI() if hasattr(F, "FastAPI") else F.App()

        @app.post("/login")
        def login(username: str = Fm(), password: str = Fm()):
            return {"u": username}

        return app

    with FA(build(fastapi, Form)) as fa, LC(build(lancelot, LForm)) as lc:
        f = fa.post("/login", data={"username": "a", "password": "b"})
        l = lc.post("/login", data={"username": "a", "password": "b"})
        assert l.status_code == f.status_code == 200
        assert l.content == f.content
        # missing required form field → 422 byte-identical
        f2 = fa.post("/login", data={"username": "a"})
        l2 = lc.post("/login", data={"username": "a"})
        assert l2.status_code == f2.status_code == 422
        assert l2.content == f2.content
```

- [ ] **Step 2: Verify fail** — `lancelot.params.Form` missing / form not parsed.

- [ ] **Step 3: Implement**
  - `lancelot/params.py`: `class Form(<existing field marker>)` with `in_="form"`, default factory mirroring `Query`/`Body` (alias/default/required semantics identical to FastAPI scalar Form).
  - `lancelot/di.py`: `_raw_for` — for `in_="form"`, parse the request body once (`urllib.parse.parse_qs`) when `content-type` is `application/x-www-form-urlencoded`; cache on the request; pull `key`. Bare-scalar Form → `loc=["body","<name>"]` on error (deep/01 line 40/214).
  - `lancelot/asgi.py`: if a route has form fields and the body is present but the content-type is unparseable for the declared form (e.g. body present, not urlencoded, not the json the route expects), return `400 {"detail":"There was an error parsing the body"}` (deep/01 line 175/215 — MUST-6). `multipart/form-data` is NOT handled (no python-multipart) → ledgered.

- [ ] **Step 4: Verify pass** — PASS (200 + 422 byte-identical to FastAPI).
- [ ] **Step 5: Full suite** — green.
- [ ] **Step 6: Commit** — `feat(m1-p6): urlencoded Form params + unparseable-body 400 (MUST-6)`.

---

### Task 6: `__init__` exports + dependency-light guard

**Files:** Modify `lancelot/__init__.py`; Test same file.

- [ ] **Step 1: Failing test**

```python
def test_imports_and_dependency_light():
    import subprocess, sys
    code = ("import sys, lancelot;"
            "lancelot.WebSocket; lancelot.WebSocketDisconnect;"
            "lancelot.StaticFiles;"
            "bad=[m for m in ('starlette','fastapi','httpx','anyio',"
            "'pydantic_settings','python_multipart') if m in sys.modules];"
            "print('CLEAN' if not bad else ('LEAK',bad))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True)
    assert out.stdout.strip() == "CLEAN", out.stdout + out.stderr
```

- [ ] **Step 2-4:** Add lazy `__getattr__` entries for `WebSocket`, `WebSocketDisconnect`, `StaticFiles` + `__all__`; verify CLEAN (modules import only stdlib at top); PASS.
- [ ] **Step 5: Commit** — `feat(m1-p6): lazy WS/StaticFiles exports + dependency-light guard`.

---

### Task 7: Conformance seed + honest RED ledger + clean-sweep + review + tag/push

**Files:** Create `conformance/vendor/quarantine/test_p6_longtail.py`.

- [ ] **Step 1: RED ledger**

```python
"""M1-P6 LEDGER (quarantined, ratchet-excluded). M1-P6 ships WS/static/
settings/urlencoded-forms byte-faithfully on the Python ASGI seam (the
path the oracle exercises). RED = out of M1-P6 scope:
  1. Rust src/ws/mod.rs (tokio-tungstenite) + src/static_files.rs
     (tower-http, sendfile(2)) wired into the Rust hyper path — spec
     §3-69 unification.
  2. multipart/form-data + file uploads (UploadFile) — needs
     python-multipart (NOT a pydantic-only runtime dep); urlencoded
     forms ship in M1-P6.
  3. WS subprotocol negotiation, permessage-deflate, ws backpressure
     bound semantics (WBS U13 done-when "bounded channel" is the Rust
     mpsc — §3-69).
  4. Static: directory index/HTML mode, multi-range, sendfile(2)
     zero-copy (tower-http) — single-range + conditional ship in P6.
Graduate as M3 + the §3-69 unification land."""
import pytest


def test_rust_ws_and_static_unification_S369():
    pytest.fail("spec §3-69: Rust src/ws/mod.rs + src/static_files.rs")


def test_multipart_and_file_upload_needs_python_multipart():
    pytest.fail("multipart/form-data + UploadFile (python-multipart) — "
                "urlencoded forms ship in M1-P6")


def test_ws_subprotocol_and_permessage_deflate():
    pytest.fail("WS subprotocol negotiation + permessage-deflate (§3-69)")
```

- [ ] **Step 2:** Verify RED + `make m1-conformance` still 11/11=100 (quarantine excluded).
- [ ] **Step 3:** Full regression: `pytest tests/ -q`, `make m1-conformance`, `make m1-spine`, `make m0`, import-clean; Rust clean-sweep (P6 pure-Python: `git diff --stat m1-p5-green -- 'crates/**/*.rs'` empty; `cargo clippy -- -D warnings` rc=0).
- [ ] **Step 4:** Dispatch a fresh review agent (same discipline as M1-P4/P5): scope = `websockets.py`, `staticfiles.py`, `settings.py`, `asgi.py`, `routing.py`, `di.py`, `params.py` deltas. Must independently verify vs real Starlette/FastAPI: WS echo+param+1008 close code & reason bytes; StaticFiles ETag/Range/304/**path-traversal** (no escape — security), header wire order; settings cached + boot-crash; urlencoded 200/422 byte-identical + unparseable→400; no auth/DoS regression (WS unbounded recv, static traversal/symlink, range integer overflow); ledger honesty + ratchet exclusion; dependency-light guard. Verdict TAG-READY/NOT-READY + file:line fixes. Fix blockers via TDD; re-confirm.
- [ ] **Step 5:** Tag `m1-p6-green`, `git push --follow-tags`, update `~/.claude/projects/-Users-seventyleven-Desktop-Lancelot/memory/lancelot-m1-progress.md` (P6 done; refresh ledger; next = P7).

---

## Self-Review

**1. Spec coverage:** U13 WS (echo text+binary, `/ws/{id}` param, DI, 1008 on validation, backpressure-bound ledgered §3-69) → T1/T2/T7. U14 StaticFiles (ETag/Last-Modified/Range/304/traversal-guard; multi-range/sendfile/index ledgered) → T3/T7. U19 Settings (cached singleton, boot-crash, no runtime dep) → T4. Carry-forward MUST-6 (urlencoded Form + unparseable→400; multipart ledgered) → T5. MUST-7 WS-1008 → T2.

**2. Placeholder scan:** none — every step has full code/exact paths/exact commands/expected output, except the ETag/Last-Modified recipe in T3 which is explicitly "match the real lib in Step 4" (a calibrate-against-oracle step, the project's established method — not a placeholder, it has a concrete starting recipe + a byte-equality acceptance gate).

**3. Type consistency:** `WebSocket`/`WebSocketDisconnect` (websockets.py) used identically in di.py (`__ws__` slot, same tagged-tuple pattern as M1-P5 `__bg__`), asgi.py, tests. `StaticFiles(directory=...)` + `app.mount(prefix, app)` consistent T3/T6/T7. `Form` extends the existing field marker (same alias/default/required contract as `Query`/`Body` from M1-P2). `settings_provider(cls, *, eager=False)` consistent T4. The `request_params` tagged-tuple binding is the exact mechanism shipped in M1-P5 (`("__bg__",name)`) — extended with `("__ws__",name)`, backward compatible.

**Risk notes:** WS-1008 reason must be `jsonable_encoder(exc.errors())` byte-shape (deep/01 line 93) — calibrate the reason string against real FastAPI/Starlette in T2 Step 4. Path-traversal guard is security-critical — T3 + the review agent must prove no escape (realpath containment + symlink). Header wire order on static/304/206 follows the M1-P4 Starlette init_headers invariant (content-length then content-type last; explicit headers first) — assert ordered where the oracle differs. multipart/file-upload and the Rust WS/static realizations are honestly ledgered RED, never silently passing — consistent with the M1-P3/P4/P5 discipline.
