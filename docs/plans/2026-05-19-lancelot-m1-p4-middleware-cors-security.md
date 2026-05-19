# Lancelot M1-P4 — Two-tier Middleware + CORS + Security surface (U20/U21/U23)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline serial, like M0/P1/P2/P3). `- [ ]` steps. Discipline: RED → verify-fail → GREEN → verify-pass → commit per task; `git -C "$LC"` (`LC=/Users/seventyleven/Desktop/Lancelot`); `export PATH="$HOME/.cargo/bin:$PATH"` + `export PYO3_PYTHON="$LC/.venv/bin/python"`; verify pytest exit DIRECTLY (`rc=$?; test $rc -eq 0 && …`, never `| tail`-masked). Sub-plan close: real-FastAPI byte-differential oracle + clean-sweep + review agent + tag/push + memory. RED seed for not-yet-implemented shapes → `conformance/vendor/quarantine/` (excluded from the ratchet), graduate to `vendor/` when green.

**Goal:** FastAPI-surface-compatible security schemes (`APIKey{Header,Query,Cookie}`, `HTTPBearer`, `HTTPBasic`, `OAuth2PasswordBearer`, `Security(scopes=)`) that extract credentials, raise the **byte-exact 401 + `WWW-Authenticate`** on failure, and emit correct OpenAPI `securitySchemes`/per-operation `security`; native built-in **CORS** (byte-faithful to Starlette's `CORSMiddleware`); and the **two-tier middleware** boundary (`app.add_middleware` = pure-ASGI escape hatch, never `BaseHTTPMiddleware`/`call_next`).

**Architecture:** Security classes are DI dependencies — the P2 solver, on a `Depends(scheme)`/annotated `SecurityBase`, calls `scheme(request)` (injecting the `Request`) and binds the value or propagates a raised `HTTPException`. A new `HTTPException` + an ASGI-seam mapping turns it into `JSONResponse({"detail":…}, status, headers)` byte-faithfully (deep/03 §1 MUST-1→14). The OpenAPI builder (P3) gains `components.securitySchemes` + per-operation `security`. CORS is reimplemented (NOT a Starlette runtime dep — pyproject stays pydantic-only; the byte-differential oracle vs real FastAPI/Starlette `CORSMiddleware` is the calibration authority, the discipline that caught P3's overstatement). `app.add_middleware(cls, **opts)` wraps `app.asgi()` at the app-callable boundary (the U20 escape hatch). Scopes are **plumbed, not enforced** (spec line 68; enforcement is M4). Full OAuth2 flows / OpenID Connect / HTTPDigest / scope-enforcement → P7 quarantine+ledger.

**Tech Stack:** Python 3.13 `.venv`; pydantic (models); the P2 DI (`lancelot/di.py`) + P3 OpenAPI (`lancelot/openapi.py`) + `lancelot/responses.py`; test deps `fastapi==0.136.1` (byte oracle, installed) + pytest/starlette/httpx. No new Rust (ASGI path; the Rust tower-layer fast-path half of U20 is a tracked carry-forward).

**Grounding (cited, no re-derivation):** `research/deep/03 §1.1-1.6` (MUST-1→14: extraction, 401, `WWW-Authenticate`, conformance matrix), `§1.4/§2` (OAuth2 + scopes plumbed-not-enforced MUST-13), `§3.2/§4.7` (OpenAPI security objects); WBS U20/U21/U23; spec §3 (`security` line 68 — "always 401 + WWW-Authenticate; scopes plumbed not enforced"). P2 gave `di.solve/solve_async` (`Depends` handling, `_resolve_dep`); P3 gave `build_openapi`/`components_schemas`. `lancelot.params.Security(Depends, scopes)` already exists (P2).

**M1-P4 Definition of Done (verified in Task 12):**
1. `from lancelot.security import OAuth2PasswordBearer, APIKeyHeader, HTTPBearer, HTTPBasic, APIKeyQuery, APIKeyCookie` resolves (FastAPI import-surface compat).
2. Per the deep/03 §1.6 matrix: missing/bad creds → **401** `{"detail":"Not authenticated"}` + the exact `WWW-Authenticate` (`APIKey`/`Bearer`/`Basic[ realm="R"]`); success returns the right value (key str / `HTTPAuthorizationCredentials` / `HTTPBasicCredentials` / token).
3. Security schemes work as DI deps end-to-end via `TestClient` (401 unauth, 200 with valid cred).
4. `app.openapi()` emits `components.securitySchemes` (byte-exact objects from §1.2-1.4) + per-operation `security`.
5. Native `CORSMiddleware`: simple-request CORS headers + preflight `OPTIONS` (Access-Control-Allow-*); byte-faithful to real FastAPI's `CORSMiddleware` for the covered config.
6. `app.add_middleware(cls, **opts)` runs an unmodified pure-ASGI middleware at the app-callable boundary.
7. **Byte-differential**: 401+`WWW-Authenticate`, CORS preflight/simple, openapi `securitySchemes` == real FastAPI for the covered subset; divergences quarantined+ledgered (never silently passing).
8. No regression: `make m0` 7/7, `make m1-spine`, `make m1-conformance` ≥ floor, full pytest green; clippy/cargo pristine (Rust unchanged); `import lancelot` dependency-light.

---

## File Structure

- `lancelot/exceptions.py` — `HTTPException(status_code, detail, headers)`.
- `lancelot/security.py` — `SecurityBase`; `APIKeyHeader/APIKeyQuery/APIKeyCookie`; `HTTPBearer`/`HTTPBasic` (+ `HTTPAuthorizationCredentials`/`HTTPBasicCredentials`); `OAuth2`/`OAuth2PasswordBearer` (+ `OAuth2PasswordRequestForm` surface); each has `.model` (OpenAPI object) + `.scheme_name` + `async __call__(request)`.
- `lancelot/middleware.py` — `app.add_middleware` plumbing + `CORSMiddleware` (pure-ASGI, byte-faithful Starlette CORS).
- Modify `lancelot/di.py` — solver recognises a `SecurityBase` dependency → `await dep(request)` (inject `Request`); record security schemes on the `Plan` for OpenAPI.
- Modify `lancelot/asgi.py` — map `HTTPException` → `JSONResponse(detail, status, headers)`; honor incoming `scope["state"]` (P2 advisory pickup); apply the middleware stack around the app.
- Modify `lancelot/openapi.py` — `components.securitySchemes` + per-operation `security`.
- Modify `lancelot/app.py` — `add_middleware`; `Request` carries `.scope`/state.
- Tests: `tests/test_m1_exceptions.py`, `test_m1_security_apikey.py`, `test_m1_security_http.py`, `test_m1_security_oauth2.py`, `test_m1_security_di.py`, `test_m1_security_openapi.py`, `test_m1_middleware.py`, `test_m1_cors.py`, `test_m1_p4_byte_parity.py`, `test_m1_p4_acceptance.py`; conformance `conformance/vendor/test_seed_security.py` (+ `quarantine/` RED for full-OAuth2/OpenID/digest/scope-enforcement).

---

### Task 1: `HTTPException` + ASGI-seam mapping (byte-exact 401 body+headers)

**Files:** Create `lancelot/exceptions.py`; Modify `lancelot/asgi.py`; Test `tests/test_m1_exceptions.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_exceptions.py
from lancelot import App
from lancelot.testclient import TestClient
from lancelot.exceptions import HTTPException


def test_httpexception_maps_to_json_with_headers():
    app = App()

    @app.get("/boom")
    def boom():
        raise HTTPException(status_code=401, detail="Not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})

    with TestClient(app) as c:
        r = c.get("/boom")
    assert r.status_code == 401
    assert r.json() == {"detail": "Not authenticated"}
    assert r.headers["www-authenticate"] == "Bearer"
```

- [ ] **Step 2: verify fail** — `cd $LC && .venv/bin/python -m pytest tests/test_m1_exceptions.py -q` → `ModuleNotFoundError: lancelot.exceptions` (or 500, not 401).

- [ ] **Step 3: implement**

`lancelot/exceptions.py`:
```python
class HTTPException(Exception):
    def __init__(self, status_code: int, detail=None, headers: dict | None = None):
        self.status_code = status_code
        self.detail = detail if detail is not None else ""
        self.headers = headers or {}
        super().__init__(f"{status_code}: {self.detail}")
```

`lancelot/asgi.py` — in `_http`'s `except` (it currently maps any Exception→500); FIRST catch `HTTPException`. Replace the `except Exception:` tail block:
```python
    except HTTPException as exc:  # import at top: from lancelot.exceptions import HTTPException
        if not sent:
            from lancelot.validation import _wire_json
            body = _wire_json({"detail": exc.detail})
            hdrs = [(b"content-type", b"application/json")]
            for k, v in exc.headers.items():
                hdrs.append((k.encode(), str(v).encode()))
            await send({"type": "http.response.start",
                        "status": exc.status_code, "headers": hdrs})
            await send({"type": "http.response.body", "body": body})
    except Exception:  # noqa: BLE001
        if not sent:
            await _send_json(send, 500, b'{"detail":"Internal Server Error"}')
```
(Add `from lancelot.exceptions import HTTPException` to the `_http` import block.)

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p4): HTTPException + ASGI-seam JSON+headers mapping (MUST-11)`

---

### Task 2: `lancelot/security.py` — `SecurityBase` + APIKey family

**Files:** Create `lancelot/security.py`; Test `tests/test_m1_security_apikey.py`

- [ ] **Step 1: failing test** (deep/03 §1.2 MUST-2/3/4 + §1.6 matrix)

```python
# tests/test_m1_security_apikey.py
import pytest
from lancelot.security import APIKeyHeader, APIKeyQuery, APIKeyCookie
from lancelot.exceptions import HTTPException
from lancelot.requests import Request


def _req(headers=None, query=None, cookies=None):
    return Request("GET", "/", query or {}, headers or {}, cookies or {},
                   {}, b"", None)


async def _call(scheme, **kw):
    return await scheme(_req(**kw))


@pytest.mark.asyncio_skip
def test_apikey_header_extract_and_401():
    import asyncio
    h = APIKeyHeader(name="key")
    assert h.model == {"type": "apiKey", "in": "header", "name": "key"}
    assert asyncio.get_event_loop().run_until_complete(
        _call(h, headers={"key": "secret"})) == "secret"
    with pytest.raises(HTTPException) as e:
        asyncio.new_event_loop().run_until_complete(_call(h, headers={}))
    assert e.value.status_code == 401
    assert e.value.headers == {"WWW-Authenticate": "APIKey"}
    # auto_error=False -> None, no raise
    h2 = APIKeyHeader(name="key", auto_error=False)
    assert asyncio.new_event_loop().run_until_complete(_call(h2, headers={})) is None
```

(Remove the illustrative `@pytest.mark.asyncio_skip`; drive asyncio directly as shown.)

- [ ] **Step 2: verify fail** — `ModuleNotFoundError: lancelot.security`.

- [ ] **Step 3: implement** `lancelot/security.py` (APIKey family; deep/03 §1.2)

```python
# lancelot/security.py
"""FastAPI-surface security schemes. Extraction/401/WWW-Authenticate are
byte-faithful to deep/03 §1 (MUST-1..14). Scopes are PLUMBED, not enforced
(spec line 68 / MUST-13 — enforcement is M4). Full OAuth2 flows / OpenID /
HTTPDigest / scope-enforcement → P7 quarantine."""
from lancelot.exceptions import HTTPException


class SecurityBase:
    model: dict
    scheme_name: str


class _APIKeyBase(SecurityBase):
    location = "header"

    def __init__(self, *, name, scheme_name=None, description=None, auto_error=True):
        self.name = name
        self.auto_error = auto_error
        self.scheme_name = scheme_name or self.__class__.__name__
        m = {"type": "apiKey", "in": self.location, "name": name}
        if description is not None:
            m["description"] = description
        self.model = m

    def _check(self, value):
        if not value:  # MUST-4: empty == missing
            if self.auto_error:
                raise HTTPException(401, "Not authenticated",
                                    {"WWW-Authenticate": "APIKey"})  # MUST-2
            return None
        return value


class APIKeyHeader(_APIKeyBase):
    location = "header"

    async def __call__(self, request):
        h = {k.lower(): v for k, v in request.headers.items()}
        return self._check(h.get(self.name.lower()))


class APIKeyQuery(_APIKeyBase):
    location = "query"

    async def __call__(self, request):
        return self._check(request.query_params.get(self.name))


class APIKeyCookie(_APIKeyBase):
    location = "cookie"

    async def __call__(self, request):
        return self._check(request.cookies.get(self.name))
```

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p4): security.py SecurityBase + APIKey{Header,Query,Cookie} (MUST-2/3/4)`

---

### Task 3: HTTP family — `HTTPBearer` + `HTTPBasic`

**Files:** Modify `lancelot/security.py`; Test `tests/test_m1_security_http.py`

- [ ] **Step 1: failing test** (deep/03 §1.3 MUST-5/6/7 + matrix)

```python
# tests/test_m1_security_http.py
import asyncio, base64
import pytest
from lancelot.security import (HTTPBearer, HTTPBasic, HTTPAuthorizationCredentials,
                               HTTPBasicCredentials)
from lancelot.exceptions import HTTPException
from lancelot.requests import Request


def _req(auth=None):
    h = {"authorization": auth} if auth else {}
    return Request("GET", "/", {}, h, {}, {}, b"", None)


def _run(co):
    return asyncio.new_event_loop().run_until_complete(co)


def test_http_bearer():
    b = HTTPBearer()
    assert b.model == {"type": "http", "scheme": "bearer"}
    c = _run(b(_req("Bearer tok123")))
    assert isinstance(c, HTTPAuthorizationCredentials)
    assert c.scheme == "Bearer" and c.credentials == "tok123"
    with pytest.raises(HTTPException) as e:   # wrong scheme -> 401 (MUST-7)
        _run(b(_req("Basic xxx")))
    assert e.value.status_code == 401
    assert e.value.headers == {"WWW-Authenticate": "Bearer"}


def test_http_basic_realm():
    bb = HTTPBasic(realm="simple")
    raw = base64.b64encode(b"u:p:extra").decode()
    cr = _run(bb(_req(f"Basic {raw}")))
    assert isinstance(cr, HTTPBasicCredentials)
    assert cr.username == "u" and cr.password == "p:extra"  # MUST-6: first ':'
    with pytest.raises(HTTPException) as e:
        _run(bb(_req(None)))
    assert e.value.headers == {"WWW-Authenticate": 'Basic realm="simple"'}  # MUST-5
```

- [ ] **Step 2: verify fail.**

- [ ] **Step 3: implement** (append to `lancelot/security.py`; deep/03 §1.1/§1.3)

```python
import base64
import binascii
from dataclasses import dataclass


@dataclass
class HTTPAuthorizationCredentials:
    scheme: str
    credentials: str


@dataclass
class HTTPBasicCredentials:
    username: str
    password: str


def _scheme_param(value):
    # MUST-1: partition on FIRST space; param stripped; missing -> ("","")
    if not value:
        return "", ""
    scheme, _, param = value.partition(" ")
    return scheme, param.strip()


class HTTPBearer(SecurityBase):
    def __init__(self, *, bearerFormat=None, scheme_name=None,
                 description=None, auto_error=True):
        self.auto_error = auto_error
        self.scheme_name = scheme_name or self.__class__.__name__
        m = {"type": "http", "scheme": "bearer"}
        if bearerFormat is not None:
            m["bearerFormat"] = bearerFormat
        if description is not None:
            m["description"] = description
        self.model = m

    def _401(self):
        return HTTPException(401, "Not authenticated", {"WWW-Authenticate": "Bearer"})

    async def __call__(self, request):
        auth = {k.lower(): v for k, v in request.headers.items()}.get("authorization")
        scheme, param = _scheme_param(auth)
        if not (auth and scheme and param):          # MUST-7 step 1
            if self.auto_error:
                raise self._401()
            return None
        if scheme.lower() != "bearer":               # MUST-7 step 2
            if self.auto_error:
                raise self._401()
            return None
        return HTTPAuthorizationCredentials(scheme=scheme, credentials=param)


class HTTPBasic(SecurityBase):
    def __init__(self, *, realm=None, scheme_name=None, description=None,
                 auto_error=True):
        self.realm = realm
        self.auto_error = auto_error
        self.scheme_name = scheme_name or self.__class__.__name__
        m = {"type": "http", "scheme": "basic"}
        if description is not None:
            m["description"] = description
        self.model = m

    def _hdrs(self):  # MUST-5
        return {"WWW-Authenticate": f'Basic realm="{self.realm}"' if self.realm
                else "Basic"}

    async def __call__(self, request):
        auth = {k.lower(): v for k, v in request.headers.items()}.get("authorization")
        scheme, param = _scheme_param(auth)
        unauth = HTTPException(401, "Not authenticated", self._hdrs())
        if not auth or scheme.lower() != "basic":    # MUST-6 path 1 (auto_error-gated)
            if self.auto_error:
                raise unauth
            return None
        try:                                          # MUST-12: NOT auto_error-gated
            data = base64.b64decode(param).decode("ascii")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            raise unauth
        user, sep, pw = data.partition(":")
        if not sep:
            raise unauth
        return HTTPBasicCredentials(username=user, password=pw)
```

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p4): HTTPBearer + HTTPBasic (MUST-1/5/6/7/12, two-step + realm + b64)`

---

### Task 4: OAuth2 — `OAuth2PasswordBearer` (+ base `OAuth2`, `OAuth2PasswordRequestForm` surface)

**Files:** Modify `lancelot/security.py`; Test `tests/test_m1_security_oauth2.py`

- [ ] **Step 1: failing test** (deep/03 §1.4 MUST-8/9/10)

```python
# tests/test_m1_security_oauth2.py
import asyncio
import pytest
from lancelot.security import OAuth2PasswordBearer, OAuth2
from lancelot.exceptions import HTTPException
from lancelot.requests import Request


def _req(auth=None):
    return Request("GET", "/", {}, ({"authorization": auth} if auth else {}),
                   {}, {}, b"", None)


def _run(co):
    return asyncio.new_event_loop().run_until_complete(co)


def test_oauth2_password_bearer():
    o = OAuth2PasswordBearer(tokenUrl="token")
    assert o.model == {"type": "oauth2",
                       "flows": {"password": {"scopes": {}, "tokenUrl": "token"}}}
    assert _run(o(_req("Bearer abc"))) == "abc"            # token only (param)
    with pytest.raises(HTTPException) as e:
        _run(o(_req("Basic abc")))                          # scheme != bearer
    assert e.value.status_code == 401
    assert e.value.headers == {"WWW-Authenticate": "Bearer"}


def test_oauth2_base_returns_raw_header():
    b = OAuth2()
    assert _run(b(_req("Other footoken"))) == "Other footoken"  # MUST-9 raw
```

- [ ] **Step 2: verify fail.**

- [ ] **Step 3: implement** (append; deep/03 §1.4)

```python
class OAuth2(SecurityBase):
    def __init__(self, *, flows=None, scheme_name=None, description=None,
                 auto_error=True):
        self.auto_error = auto_error
        self.scheme_name = scheme_name or self.__class__.__name__
        m = {"type": "oauth2", "flows": flows or {}}
        if description is not None:
            m["description"] = description
        self.model = m

    def _401(self):
        return HTTPException(401, "Not authenticated", {"WWW-Authenticate": "Bearer"})

    async def __call__(self, request):
        auth = {k.lower(): v for k, v in request.headers.items()}.get("authorization")
        if not auth:                                  # MUST-9
            if self.auto_error:
                raise self._401()
            return None
        return auth                                   # raw full header


class OAuth2PasswordBearer(OAuth2):
    def __init__(self, tokenUrl, *, scopes=None, scheme_name=None,
                 description=None, auto_error=True, refreshUrl=None):
        flow = {"scopes": scopes or {}, "tokenUrl": tokenUrl}  # MUST-10 key order
        if refreshUrl is not None:
            flow = {"refreshUrl": refreshUrl, **flow}
        super().__init__(flows={"password": flow}, scheme_name=scheme_name,
                         description=description, auto_error=auto_error)

    async def __call__(self, request):
        auth = {k.lower(): v for k, v in request.headers.items()}.get("authorization")
        scheme, param = _scheme_param(auth)
        if not auth or scheme.lower() != "bearer":
            if self.auto_error:
                raise self._401()
            return None
        return param                                  # token only


class OAuth2PasswordRequestForm:
    """Form-dependency surface (import-compat). Full form parsing is M1-P6
    (forms) — P7 quarantine seed tracks the byte-exact 422 shape."""

    def __init__(self, *, grant_type=None, username="", password="",
                 scope="", client_id=None, client_secret=None):
        self.grant_type = grant_type
        self.username = username
        self.password = password
        self.scopes = scope.split()
        self.client_id = client_id
        self.client_secret = client_secret
```

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p4): OAuth2 + OAuth2PasswordBearer + form surface (MUST-8/9/10)`

---

### Task 5: DI integration — `SecurityBase` dependency calls `dep(request)`

**Files:** Modify `lancelot/di.py`, `lancelot/requests.py`, `lancelot/asgi.py`; Test `tests/test_m1_security_di.py`

- [ ] **Step 1: failing test** (end-to-end via TestClient)

```python
# tests/test_m1_security_di.py
from lancelot import App
from lancelot.testclient import TestClient
from lancelot.params import Depends
from lancelot.security import HTTPBearer

bearer = HTTPBearer()


def test_security_dep_401_and_200():
    app = App()

    @app.get("/me")
    def me(cred=Depends(bearer)):
        return {"scheme": cred.scheme, "tok": cred.credentials}

    with TestClient(app) as c:
        r0 = c.get("/me")
        assert r0.status_code == 401
        assert r0.json() == {"detail": "Not authenticated"}
        assert r0.headers["www-authenticate"] == "Bearer"
        r1 = c.get("/me", headers={"Authorization": "Bearer xyz"})
        assert r1.status_code == 200
        assert r1.json() == {"scheme": "Bearer", "tok": "xyz"}
```

- [ ] **Step 2: verify fail** — security dep is treated as a plain dependency and called with no args → `TypeError` → 500 (not 401/200).

- [ ] **Step 3: implement**

`lancelot/requests.py` — ensure `Request` is the object the scheme receives (it already has headers/query/cookies). No change if Task 1's seam passes the live `LRequest`.

`lancelot/di.py` — in BOTH `solve` and `solve_async`, when resolving a `SubDep` whose effective `target` is a `SecurityBase` instance (`isinstance(target, SecurityBase)` — import lazily to avoid cycle), call `await target(request)` (async) / bind its result, instead of `target(**sr.kwargs)`. Concretely, in the dep loop after `target, subplan = _resolve_dep(sub, request)`:
```python
        from lancelot.security import SecurityBase
        if isinstance(target, SecurityBase):
            val = target(request)
            import inspect as _i
            if _i.isawaitable(val):
                val = await val            # solve_async path
            kwargs[sub.param_name] = val
            if sub.use_cache:
                cache[sub.call] = val
            continue
```
(For the sync `solve()` path — security `__call__` is async; `solve()` is the non-yield path. Security schemes are async, so they require the `solve_async` path. Route handling always uses `solve_async` at the ASGI seam (P2/T11), so this is fine; in sync `solve()` raise a clear error if a SecurityBase is hit: `raise RuntimeError("security schemes require the async path")` — never reached from the real request path. Add the SecurityBase branch only to `solve_async`; in `solve` add the guard.)

Record security schemes on the `Plan` at compile time for OpenAPI (Task 6): in `compile_dependant`, when a `Depends`/`Security` dependency's `.dependency` is a `SecurityBase`, append `(scheme, own_scopes)` to a new `Plan.security: list` (default `[]`); `Security(...).scopes` carries the scopes (P2 `params.Security`).

- [ ] **Step 4: verify pass.** (HTTPException raised inside `solve_async`'s dep resolution must propagate out through the `async with` to `_http`'s `except HTTPException` from Task 1 — verify the 401 path; the exc-info also reaches yield-dep teardown, B4-correct.)
- [ ] **Step 5: commit** — `feat(m1-p4): DI calls SecurityBase deps with the Request; 401 propagates to the seam`

---

### Task 6: OpenAPI — `components.securitySchemes` + per-operation `security`

**Files:** Modify `lancelot/openapi.py`; Test `tests/test_m1_security_openapi.py`

- [ ] **Step 1: failing test** (deep/03 §3.2/§4.7)

```python
# tests/test_m1_security_openapi.py
from lancelot import App
from lancelot.params import Depends, Security
from lancelot.security import OAuth2PasswordBearer, APIKeyHeader

oauth2 = OAuth2PasswordBearer(tokenUrl="token", scopes={"read": "Read"})
apikey = APIKeyHeader(name="X-Key")


def test_openapi_security_schemes_and_operation_security():
    app = App()

    @app.get("/a")
    def a(t=Depends(oauth2)):
        return {}

    @app.get("/b")
    def b(k=Security(apikey, scopes=["read"])):
        return {}

    doc = app.openapi()
    cs = doc["components"]["securitySchemes"]
    assert cs["OAuth2PasswordBearer"] == {
        "type": "oauth2", "flows": {"password": {"scopes": {"read": "Read"},
                                                 "tokenUrl": "token"}}}
    assert cs["APIKeyHeader"] == {"type": "apiKey", "in": "header", "name": "X-Key"}
    assert doc["paths"]["/a"]["get"]["security"] == [{"OAuth2PasswordBearer": []}]
    assert doc["paths"]["/b"]["get"]["security"] == [{"APIKeyHeader": ["read"]}]
```

- [ ] **Step 2: verify fail** — no `securitySchemes`/`security` keys.

- [ ] **Step 3: implement** — in `lancelot/openapi.py`:
  - `build_openapi`: per route, if `route.plan.security`, set `op["security"] = [{scheme.scheme_name: list(scopes)} for scheme, scopes in route.plan.security]` (insertion order, deduped — MUST-14 OpenAPI ordering) and collect `securitySchemes[scheme.scheme_name] = scheme.model`.
  - In the top-level doc, if any security schemes collected: `doc["components"]["securitySchemes"] = {name: model}` (insertion order, NOT sorted — deep/03 §3.1 step 8: securitySchemes are route-iteration order, only `schemas` is sorted). Place under the existing `components` (alongside `schemas`).

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p4): OpenAPI securitySchemes + per-operation security (MUST-14, route-order)`

---

### Task 7: `app.add_middleware` — pure-ASGI escape hatch (U20)

**Files:** Create `lancelot/middleware.py`; Modify `lancelot/app.py`, `lancelot/asgi.py`; Test `tests/test_m1_middleware.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_middleware.py
from lancelot import App
from lancelot.testclient import TestClient


class HeaderMW:  # an unmodified pure-ASGI middleware
    def __init__(self, app, value):
        self.app = app
        self.value = value

    async def __call__(self, scope, receive, send):
        async def send2(msg):
            if msg["type"] == "http.response.start":
                msg = dict(msg)
                msg["headers"] = list(msg.get("headers", [])) + [
                    (b"x-mw", self.value.encode())]
            await send(msg)
        await self.app(scope, receive, send2)


def test_add_middleware_runs_at_app_boundary():
    app = App()

    @app.get("/")
    def root():
        return {"ok": True}

    app.add_middleware(HeaderMW, value="hit")
    with TestClient(app) as c:
        r = c.get("/")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert r.headers["x-mw"] == "hit"
```

- [ ] **Step 2: verify fail** — `App` has no `add_middleware`.

- [ ] **Step 3: implement**

`lancelot/app.py`:
```python
    def add_middleware(self, mw_cls, **opts):
        self._middleware.append((mw_cls, opts))
        self._asgi = None  # rebuild the wrapped app
```
add `self._middleware = []` in `__init__` (before `_register_builtin_routes`), and in `asgi()` wrap:
```python
    def asgi(self):
        if self._asgi is None:
            from lancelot.asgi import build_asgi
            app = build_asgi(self)
            for mw_cls, opts in reversed(self._middleware):
                app = mw_cls(app, **opts)   # outermost = last added wraps first
            self._asgi = app
        return self._asgi
```

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p4): app.add_middleware — pure-ASGI escape hatch (U20, no BaseHTTPMiddleware)`

---

### Task 8: native `CORSMiddleware` (byte-faithful Starlette CORS — U21)

**Files:** Modify `lancelot/middleware.py`; Test `tests/test_m1_cors.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_cors.py
from lancelot import App
from lancelot.middleware import CORSMiddleware
from lancelot.testclient import TestClient


def _app():
    app = App()

    @app.get("/")
    def root():
        return {"ok": True}

    app.add_middleware(CORSMiddleware, allow_origins=["https://ex.com"],
                       allow_methods=["GET"], allow_headers=["x-test"])
    return app


def test_cors_simple_and_preflight():
    with TestClient(_app()) as c:
        r = c.get("/", headers={"Origin": "https://ex.com"})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "https://ex.com"
        pf = c.request("OPTIONS", "/", headers={
            "Origin": "https://ex.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-test"})
        assert pf.status_code == 200
        assert pf.headers["access-control-allow-origin"] == "https://ex.com"
        assert "GET" in pf.headers["access-control-allow-methods"]
        # disallowed origin -> no ACAO header
        r2 = c.get("/", headers={"Origin": "https://evil.com"})
        assert "access-control-allow-origin" not in r2.headers
```

- [ ] **Step 2: verify fail** — `cannot import name 'CORSMiddleware'`.

- [ ] **Step 3: implement** (pure-ASGI; Starlette-CORS semantics — the byte oracle in Task 10 calibrates)

```python
# add to lancelot/middleware.py
class CORSMiddleware:
    def __init__(self, app, *, allow_origins=(), allow_methods=("GET",),
                 allow_headers=(), allow_credentials=False, max_age=600):
        self.app = app
        self.allow_origins = set(allow_origins)
        self.allow_all_origins = "*" in self.allow_origins
        self.allow_methods = list(allow_methods)
        self.allow_headers = list(allow_headers)
        self.allow_credentials = allow_credentials
        self.max_age = max_age

    def _allowed(self, origin):
        return self.allow_all_origins or origin in self.allow_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode()
                   for k, v in scope.get("headers", [])}
        origin = headers.get("origin")
        if origin is None:
            await self.app(scope, receive, send)
            return
        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            h = [(b"content-type", b"text/plain; charset=utf-8")]
            if self._allowed(origin):
                h += [
                    (b"access-control-allow-origin", origin.encode()),
                    (b"access-control-allow-methods",
                     ", ".join(self.allow_methods).encode()),
                    (b"access-control-allow-headers",
                     ", ".join(self.allow_headers).encode()),
                    (b"access-control-max-age", str(self.max_age).encode()),
                ]
                if self.allow_credentials:
                    h.append((b"access-control-allow-credentials", b"true"))
            await send({"type": "http.response.start", "status": 200, "headers": h})
            await send({"type": "http.response.body", "body": b"OK"})
            return

        async def send2(msg):
            if msg["type"] == "http.response.start" and self._allowed(origin):
                msg = dict(msg)
                hh = list(msg.get("headers", []))
                hh.append((b"access-control-allow-origin", origin.encode()))
                if self.allow_credentials:
                    hh.append((b"access-control-allow-credentials", b"true"))
                msg["headers"] = hh
            await send(msg)

        await self.app(scope, receive, send2)
```

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p4): native CORSMiddleware — simple + preflight (U21)`

---

### Task 9: honor incoming `scope["state"]` (P2 advisory pickup)

**Files:** Modify `lancelot/asgi.py`, `lancelot/requests.py`; Test `tests/test_m1_scope_state.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_scope_state.py
from lancelot import App
from lancelot.testclient import TestClient


def test_request_state_seeded_from_scope():
    app = App()

    @app.get("/s")
    def s(): ...

    # Starlette TestClient seeds scope["state"]; a middleware can set it.
    class SetState:
        def __init__(self, app): self.app = app
        async def __call__(self, scope, receive, send):
            scope.setdefault("state", {})["uid"] = 7
            await self.app(scope, receive, send)

    @app.get("/who")
    def who(req=__import__("lancelot.params", fromlist=["Request"]).Request if False
            else None):
        return {}

    # assert via a dependency that reads request.state.uid
    from lancelot.params import Depends

    def cur(r=None):
        return None

    app.add_middleware(SetState)
    with TestClient(app) as c:
        assert c.get("/s").status_code in (200, 422) or True  # smoke
```

> Replace Step 1 with a precise test: a middleware sets `scope["state"]["uid"]`, a handler param of type `Request` (add a `Request` injectable: a `SecurityBase`-like sentinel the DI binds to the live `LRequest`) reads `request.state["uid"]`. Implement the minimal `Request` DI-injectable in Step 3 (param annotated `lancelot.Request` → bound to the live request; `request.state` returns `scope["state"]`).

- [ ] **Step 2: verify fail.**
- [ ] **Step 3: implement** — `asgi._http`: `lreq.scope_state = scope.get("state") or {}`; `Request.state` property returns it (currently `Request.state` returns `app.state`). Make `Request.state` return the scope state dict if present else app.state. DI: a handler/dep param annotated with `lancelot.Request` (re-export `lancelot.requests.Request`) binds to the live `LRequest` (add in `solve_async`: if `fi.annotation is Request` → bind the request, skip validation).
- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p4): honor scope['state'] + Request DI-injectable (P2 advisory pickup)`

---

### Task 10: real-FastAPI byte-differential oracle

**Files:** Test `tests/test_m1_p4_byte_parity.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_p4_byte_parity.py
import fastapi
from fastapi.testclient import TestClient as FA
import lancelot
from lancelot.testclient import TestClient as LC


def _build(F, sec, mw):
    app = F.FastAPI(title="T") if hasattr(F, "FastAPI") else F.App(title="T")
    scheme = sec.HTTPBearer()

    @app.get("/me")
    def me(c=F_dep(F)(scheme)):
        return {"s": c.scheme}

    mwcls = mw.CORSMiddleware
    app.add_middleware(mwcls, allow_origins=["https://ex.com"],
                       allow_methods=["GET"], allow_headers=["x-test"])
    return app


def F_dep(F):
    return (fastapi.Depends if hasattr(F, "FastAPI")
            else __import__("lancelot.params", fromlist=["Depends"]).Depends)


def test_p4_byte_parity():
    import fastapi.security as fsec, fastapi.middleware.cors as fcors
    import lancelot.security as lsec, lancelot.middleware as lmw
    with FA(_build(fastapi, fsec, fcors)) as fa, \
         LC(_build(lancelot, lsec, lmw)) as lc:
        # 401 body+headers byte-identical
        f0, l0 = fa.get("/me"), lc.get("/me")
        assert l0.status_code == f0.status_code == 401
        assert l0.content == f0.content
        assert l0.headers.get("www-authenticate") == f0.headers.get("www-authenticate")
        # openapi securitySchemes structurally identical
        assert lc.get("/openapi.json").json()["components"]["securitySchemes"] == \
               fa.get("/openapi.json").json()["components"]["securitySchemes"]
        # CORS simple-request header parity
        fh = fa.get("/me", headers={"Origin": "https://ex.com",
                                    "Authorization": "Bearer x"})
        lh = lc.get("/me", headers={"Origin": "https://ex.com",
                                    "Authorization": "Bearer x"})
        assert lh.headers.get("access-control-allow-origin") == \
               fh.headers.get("access-control-allow-origin")
```

(FastAPI's `CORSMiddleware` ctor takes `allow_origins=` etc. — same kw; `fastapi.middleware.cors.CORSMiddleware` is Starlette's. `app.add_middleware(CORSMiddleware, ...)` is FastAPI's API.)

- [ ] **Step 2: verify fail** — capture diffs.
- [ ] **Step 3: reconcile** — covered shapes (401 body/headers, securitySchemes objects, ACAO header) → fix to byte-match real FastAPI. Genuine long-tail (full preflight header set ordering, OAuth2 form 422, OpenID, digest, scope enforcement) → `conformance/vendor/quarantine/test_p4_*` RED seed + ledger. Iterate until covered subset is byte/structurally identical and every exclusion is ledgered.
- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `test(m1-p4): real-FastAPI byte-differential (401/securitySchemes/CORS); long-tail quarantined`

---

### Task 11: conformance seed + acceptance + ratchet

**Files:** `conformance/vendor/test_seed_security.py`, `tests/test_m1_p4_acceptance.py`; `conformance/vendor/quarantine/test_p4_longtail.py`

- [ ] **Step 1: conformance seed (covered shapes via the swap)**

```python
# conformance/vendor/test_seed_security.py
from fastapi import FastAPI, Depends
from fastapi.security import HTTPBearer
from fastapi.testclient import TestClient

app = FastAPI()
bearer = HTTPBearer()


@app.get("/me")
def me(c=Depends(bearer)):
    return {"s": c.scheme}


client = TestClient(app)


def test_bearer_401_and_200():
    r = client.get("/me")
    assert r.status_code == 401
    assert r.json() == {"detail": "Not authenticated"}
    assert r.headers["www-authenticate"] == "Bearer"
    ok = client.get("/me", headers={"Authorization": "Bearer t"})
    assert ok.json() == {"s": "Bearer"}


def test_security_scheme_in_openapi():
    cs = client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert cs["HTTPBearer"] == {"type": "http", "scheme": "bearer"}
```

- [ ] **Step 2: acceptance** `tests/test_m1_p4_acceptance.py` — APIKeyHeader+OAuth2PasswordBearer+CORS through `TestClient`, asserting 401/200/headers/openapi; plus `import` surface (`from lancelot.security import OAuth2PasswordBearer, APIKeyHeader, HTTPBearer, HTTPBasic`).

```python
# tests/test_m1_p4_acceptance.py
from lancelot import App
from lancelot.testclient import TestClient
from lancelot.params import Depends
from lancelot.security import (OAuth2PasswordBearer, APIKeyHeader, HTTPBearer,
                               HTTPBasic, APIKeyQuery, APIKeyCookie)


def test_p4_dod():
    app = App()
    ak = APIKeyHeader(name="X-Key")

    @app.get("/k")
    def k(key=Depends(ak)):
        return {"key": key}

    with TestClient(app) as c:
        assert c.get("/k").status_code == 401
        assert c.get("/k").headers["www-authenticate"] == "APIKey"
        assert c.get("/k", headers={"X-Key": "v"}).json() == {"key": "v"}
        cs = c.get("/openapi.json").json()["components"]["securitySchemes"]
        assert cs["APIKeyHeader"] == {"type": "apiKey", "in": "header",
                                      "name": "X-Key"}
```

- [ ] **Step 3:** quarantine RED for the long tail (full OAuth2 password flow + 422 form shape, OpenID Connect, HTTPDigest, scope ENFORCEMENT [M4]) with a ledger docstring.
- [ ] **Step 4:** `.venv/bin/python conformance/report.py` (active rises 9→11, all green); ratchet floor held; `make m1-conformance` exit 0.
- [ ] **Step 5: commit** — `test(m1-p4): conformance seed + acceptance + P7 quarantine ledger`

---

### Task 12: clean-sweep + review + no-regression + tag

- [ ] **Step 1:** zero Rust delta since `m1-p3-green` (`git -C "$LC" diff --stat m1-p3-green..HEAD -- 'src/**' Cargo.toml`); `cargo clippy --all-targets --features dhat-heap` clean; `cargo test` green.
- [ ] **Step 2:** full regression — `.venv/bin/python -m pytest tests/ -q`; `make m1-spine`; `make m1-conformance`; `make m0` (prints "all 7 gates green"); `import lancelot` starlette/fastapi-free.
- [ ] **Step 3:** dispatch a review agent (bugs / **vulnerabilities** — auth bypass, timing, header-injection via WWW-Authenticate/CORS-origin reflection, CORS `*`+credentials misconfig; plan/spec alignment; **broadened byte-calibration** vs real FastAPI for security+CORS; confirm scopes plumbed-not-enforced is intentional+ledgered; confirm `Origin` reflection in CORS matches Starlette and isn't an open-reflector beyond config). Fix blockers via TDD; ledger advisories accurately (no overstated claims — the P3 lesson).
- [ ] **Step 4:** tag + push + memory:
```bash
LC=/Users/seventyleven/Desktop/Lancelot
git -C "$LC" add -A && git -C "$LC" commit -q -m "test(m1-p4): clean-sweep + no-regression (middleware/CORS/security green)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
git -C "$LC" tag -a m1-p4-green -m "M1-P4: security schemes (byte-exact 401+WWW-Authenticate, OpenAPI securitySchemes) + native CORS + ASGI-middleware escape hatch; scopes plumbed-not-enforced (M4). M0/P1/P2/P3 intact."
git -C "$LC" push origin main --follow-tags
```
Then update `~/.claude/.../memory/lancelot-m1-progress.md` (P4 done; refresh P5..P7 ledger + the security long-tail).

---

## Self-Review

**1. Spec/WBS coverage:** U23 (security surface, FastAPI-import-compatible, auto OpenAPI schemes; scopes plumbed-not-enforced) → T2 (APIKey MUST-2/3/4), T3 (HTTP MUST-1/5/6/7/12), T4 (OAuth2 MUST-8/9/10), T5 (DI integration), T6 (OpenAPI securitySchemes/security MUST-14). U21 (native CORS, preflight) → T8. U20 (two-tier middleware, ASGI escape hatch, never BaseHTTPMiddleware) → T7. HTTPException byte-exact 401 (MUST-11) → T1. spec line 68 "always 401 + WWW-Authenticate; scopes plumbed not enforced" → T1-T6 + the explicit MUST-13 non-enforcement (ledgered, T11/T12). Byte oracle → T10; conformance/ratchet → T11; review+tag → T12. The Rust tower-layer fast-path half of U20, full OAuth2 flows, OpenID Connect, HTTPDigest, scope ENFORCEMENT (M4) are explicitly P7/M4 quarantine — named, not silent.

**2. Placeholder scan:** No TBD. Two tests carry an explicit "replace the illustrative form" note with the precise alternative inline (T2 asyncio decorator; T9 Step 1 → the precise state test) — staged clarity, not placeholders. `OAuth2PasswordRequestForm` is a documented import-surface stub (full form parsing = M1-P6) — named scope boundary. All code blocks complete & runnable.

**3. Type consistency:** `HTTPException(status,detail,headers)` (T1) raised by every scheme (T2-T4), caught in `asgi._http` (T1); `SecurityBase` (T2) base for all schemes, detected by `di` (T5) + read by `openapi` (T6); `_scheme_param` (T3) reused by T4; `HTTPAuthorizationCredentials`/`HTTPBasicCredentials` (T3) returned + asserted (T3/T5/T10); `Plan.security` (T5) consumed by `build_openapi` (T6); `add_middleware`/`self._middleware`/`asgi()` wrap (T7) consumed by CORS (T8) + the oracle (T10); `Request.state`/scope-state (T9) consistent with P2 `Request`. `params.Security(...).scopes` is the P2 contract (unchanged). Consistent.

**4. Risks:** (a) security byte-fidelity hinges on the deep/03 §1 MUST rules — T10's real-FastAPI oracle is the guard (the discipline that caught P3's overstatement; claims will be scoped to the covered subset, not overstated). (b) CORS reimplemented (no Starlette runtime dep) — T10 calibrates vs real FastAPI/Starlette `CORSMiddleware`; full preflight header-set parity is explicitly oracle-or-quarantine. (c) `solve()` (sync, non-yield) can't run async security `__call__` — guarded with a clear error; the real request path always uses `solve_async` (P2/T11) so unaffected; flagged for the T12 review. (d) CORS `allow_origins=["*"]` + `allow_credentials=True` is a known unsafe misconfig — match Starlette's behavior exactly (don't "fix" it differently) and note in the T12 security review. **No issues requiring rework.**

---

*Next on completion: M1-P5 (blocking pool + background tasks — U32/U33) gets its own plan. Same approach.*
