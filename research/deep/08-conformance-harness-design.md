# Lancelot — Conformance / Dogfood Harness Design

> The bridge that makes "instant + stable" measurable, not claimed. Lancelot's
> release gate is **"pass FastAPI's own 581-file / 74,546-line test suite + Robyn's
> integration tests against a Lancelot app."** Stability stops being a marketing
> word and becomes a CI number: *parity %*. This document is the concrete,
> reimplementable harness: directory layout, adapter code, the include/exclude
> rule for FastAPI tests, the v0 must-pass subset, the Robyn-test reuse plan,
> the CI gate, and parity tracking over time.
>
> Labels: **KNOWN** (from FastAPI/Starlette/Robyn source read this session, cited
> `path:line`) · **INFERRED** (derived from that source + Lancelot design docs)
> · **DESIGN** (our harness engineering — the deliverable).
> Sources read this session: `fastapi/fastapi/fastapi/testclient.py:1`;
> Starlette `testclient.py:1-740`; `fastapi/fastapi/tests/` (581 files, surveyed);
> `fastapi/fastapi/tests/utils.py:1-30`; `fastapi/fastapi/pyproject.toml:224-254`;
> `robyn/Robyn/robyn/testing.py:1-350`; `robyn/Robyn/integration_tests/conftest.py:1-80`;
> `Lancelot/research/DESIGN-DECISIONS.md`; `SYNTHESIS.md`;
> `design/02-deployment-cloud-native.md:947-1060` (the ASGI dual-tier decision).

---

## Executive summary (12 lines)

1. **FastAPI's `TestClient` can run UNMODIFIED against a Lancelot app — yes.**
   The reason is structural: `fastapi/testclient.py:1` is a one-line re-export of
   Starlette's `TestClient`, and Starlette's `TestClient` is a *pure in-process
   ASGI driver* (`testclient.py:199-365`) — it never opens a socket; it builds an
   ASGI `scope` dict, drives `receive`/`send`, and calls `app(scope, receive, send)`.
   Its only contract is **a spec-compliant ASGI 3.0 callable**. Lancelot's design
   already ships exactly that: `app.asgi()` returns a spec-compliant ASGI 3.0
   callable (`design/02 §6.2 seam 3`). So `TestClient(lancelot_app.asgi())` works
   with zero edits to either the TestClient or the test files.
2. The harness is therefore a **one-symbol swap**, not a port: a `conftest.py`
   that rebinds `fastapi.testclient.TestClient` to a thin wrapper which calls
   `app.asgi()` before handing the app to the real Starlette TestClient.
3. **Robyn's `TestClient` (`robyn/testing.py:117`) is the opposite** — a
   handler-direct executor that re-implements routing/middleware in Python and
   returns a custom `TestResponse`. It is **not** reusable as a driver; only
   Robyn's *integration tests* (real-socket, `conftest.py:1`) are reusable, by
   pointing them at a Lancelot server process.
4. Three test classes in FastAPI's suite: (a) **behavioral** (~95%, drive via
   TestClient/HTTP) → run unmodified through the ASGI shim; (b) **FastAPI-internal**
   (~12 files import `fastapi.dependencies.utils`, `fastapi._compat`, etc.) → these
   test FastAPI's *own private API*, not wire behavior — **excluded** (Lancelot
   re-implements those internals, so the symbols won't exist / differ by design);
   (c) **xfail-quarantine** (snapshot-pinned OpenAPI/docs HTML byte-strings) →
   included but tracked as a separate parity lane.
5. v0 must-pass subset = the table-stakes wire surface (`DESIGN-DECISIONS §3`):
   routing, typed params, Pydantic v2 validation + the exact 422 wire-shape,
   `Depends()` + `dependency_overrides`, `include_router`, lifespan, CORS, sync+async
   handlers, JSON, WebSockets, static files, background tasks, OpenAPI 3.1 +
   Swagger/ReDoc — concretely ≈ **180–220 of the 581 files** (enumerated in §6).
6. Parity % = `passed / (collected − legitimately-skipped)` per lane, written to
   a JSON artifact every CI run, plotted over time; the release gate is
   **v0-subset = 100%, full-suite ≥ target ratchet** (§9).

---

## 1. The decisive finding: why FastAPI's TestClient runs unmodified

### 1.1 The evidence chain (KNOWN)

`fastapi/fastapi/fastapi/testclient.py` is, in its entirety:

```python
from starlette.testclient import TestClient as TestClient  # noqa
```

So "FastAPI's TestClient" *is* Starlette's TestClient. There is no FastAPI-specific
test transport. Everything below is Starlette's `testclient.py` (read this session,
740 lines).

**Starlette's `TestClient` is an in-process ASGI driver, not an HTTP client over a
socket.** The proof is `_TestClientTransport.handle_request` (`testclient.py:217-365`):

- It is an `httpx.BaseTransport` subclass (`testclient.py:199`) — `TestClient`
  itself subclasses `httpx.Client` (`testclient.py:368`) with this transport
  injected (`testclient.py:394-401`). So tests get a real `httpx.Response` with
  `.status_code`, `.json()`, `.text`, `.headers`, `.cookies` — *no behavioral
  shim needed on the assertion side*.
- `handle_request` builds an ASGI **scope dict** by hand from the `httpx.Request`
  (`testclient.py:269-283` for HTTP, `:252-265` for WebSocket):
  ```python
  scope = {
      "type": "http", "http_version": "1.1",
      "method": request.method,
      "path": unquote(path),
      "raw_path": raw_path.split(b"?", 1)[0],
      "root_path": self.root_path,
      "scheme": scheme,
      "query_string": query.encode(),
      "headers": headers,                  # list[tuple[bytes,bytes]]
      "client": self.client,               # ("testclient", 50000)
      "server": [host, port],
      "extensions": {"http.response.debug": {}},
      "state": self.app_state.copy(),
  }
  ```
- It defines local `async def receive()` (`testclient.py:292-318`) yielding
  `{"type":"http.request","body":...}` then `{"type":"http.disconnect"}`, and
  `async def send(message)` (`testclient.py:320-340`) collecting
  `http.response.start` / `http.response.body`.
- It then runs the app **in-thread via an anyio blocking portal**
  (`testclient.py:342-345`):
  ```python
  with self.portal_factory() as portal:
      response_complete = portal.call(anyio.Event)
      portal.call(self.app, scope, receive, send)
  ```
  `self.app` is whatever was passed to `TestClient(app)`, normalized to ASGI3 by
  `_is_asgi3` / `_WrapASGI2` (`testclient.py:55-71, 387-392`).
- Lifespan: `with TestClient(app) as client:` drives a `{"type":"lifespan"}` scope
  through `lifespan()` / `wait_startup()` / `wait_shutdown()`
  (`testclient.py:701-739`), expecting `lifespan.startup(.complete|.failed)` and
  `lifespan.shutdown(.complete|.failed)`. **14 FastAPI test files use
  `with TestClient` (lifespan).**
- WebSockets: `websocket_connect` (`testclient.py:646-667`) builds a
  `"type":"websocket"` scope and runs `WebSocketTestSession`
  (`testclient.py:94-197`) which drives `websocket.connect/receive/send/close`
  messages through the same `app(scope, receive, send)` call.

**Conclusion (KNOWN→INFERRED):** The complete contract Starlette's TestClient
imposes on the app under test is: *be an `async def app(scope, receive, send)`
that honors the ASGI 3.0 message grammar for `http`, `websocket`, and `lifespan`
scope types.* Nothing else. No FastAPI types, no Starlette types, no sockets.

### 1.2 Why this is exactly what Lancelot already ships

`DESIGN-DECISIONS §1 fork 3` and `design/02 §6.2 seam 3` lock in:

> "Lancelot exposes `app.asgi()` returning a **spec-compliant ASGI 3.0 callable**."

That is the precise object Starlette's TestClient needs. The dual-tier decision
(`DESIGN-DECISIONS` fork #3) explicitly separates the *native Rust hot path*
(not ASGI) from the *ASGI compatibility surface* (`app.asgi()`, the migration-OUT
seam). **The conformance harness rides the compatibility surface deliberately** —
we are not testing the fast path's wire bytes via the slow path's API; we are
testing that Lancelot's *observable HTTP/WebSocket/lifespan behavior* is
byte-identical to FastAPI's, and the ASGI surface is the contractually-defined
place that behavior is exposed. (The fast path's *performance* is a separate
benchmark harness, out of scope here. Conformance = correctness parity;
benchmark = speed parity.)

**Answer to the brief's question, stated plainly:**

> **Can Lancelot's dual-tier ASGI shim (`app.asgi()`) let FastAPI's TestClient run
> unmodified against a Lancelot app? — YES.** Because (1) FastAPI's TestClient is
> Starlette's, (2) Starlette's TestClient's sole contract is "give me an ASGI 3.0
> callable", (3) `app.asgi()` is by design a spec-compliant ASGI 3.0 callable.
> The only glue needed is a `conftest.py` that makes `TestClient(lancelot_app)`
> mean `RealTestClient(lancelot_app.asgi())` — a ~30-line shim, no test edits.

### 1.3 The one structural caveat (INFERRED → must be designed for)

There is **one** place a naive `app.asgi()` would diverge from FastAPI under
TestClient, and the harness must surface it rather than hide it:

- **`scope["app"]` / `scope["state"]` / `request.app` access.** FastAPI handlers
  and dependencies routinely read `request.app`, `request.app.state`,
  `websocket.app`. Starlette's TestClient seeds `scope["state"]` from
  `self.app_state` (`testclient.py:282, 393, 702`) and FastAPI's Starlette
  populates `scope["app"]`. Lancelot's `app.asgi()` callable **must** inject an
  equivalent `scope["app"]` (a Lancelot app proxy exposing `.state`,
  `.dependency_overrides`, `.router`, `.openapi()`) and honor the incoming
  `scope["state"]`. This is a **DESIGN requirement on `app.asgi()`**, recorded
  here so the ASGI-surface implementer treats "TestClient conformance" as a
  first-class consumer, not an afterthought. (See §10, ASGI-surface obligations.)

Everything else (headers as `list[tuple[bytes,bytes]]`, `raw_path`,
`query_string` bytes, `http.disconnect` after body, `more_body` streaming,
lifespan startup/shutdown ordering, websocket message grammar) is plain ASGI 3.0
and is a hard requirement on `app.asgi()` independent of testing.

---

## 2. FastAPI's test suite — structure (KNOWN, surveyed this session)

Counts (this session, `find`/`grep` over `fastapi/fastapi/tests/`):

| Fact | Value | Evidence |
|---|---|---|
| Total `.py` test files | **581** | `find tests -name '*.py' | wc -l` |
| Total lines | **74,546** | `find tests -name '*.py' -exec cat | wc -l` |
| Top-level `test_*.py` (non-tutorial) | **206** | `ls tests/*.py` |
| `tests/test_tutorial/**` test files | **249** | `find test_tutorial -name 'test_*.py'` |
| Import `from fastapi.testclient import TestClient` | **434** | grep |
| Import `from starlette.testclient import TestClient` | **3** | grep |
| Import `from fastapi import FastAPI` directly | **134** | grep |
| Pull `app` from `docs_src.*` tutorials | **92** | grep `from docs_src` |
| Assert OpenAPI schema (`/openapi.json` / `openapi_schema`) | **290** | grep |
| Assert 422 / `"detail"` wire-shape | **233** | grep |
| Use `with TestClient(app)` (lifespan) | **14** | grep |
| WebSocket test files | **11** | grep `websocket` |
| Use `inline_snapshot` | **297** | grep |
| Use `dirty_equals` | **28** | grep |
| Use `httpx.AsyncClient` / `ASGITransport` directly | **4** | grep |
| **`conftest.py` in `tests/`** | **0** | `find tests -name conftest.py` (only `scripts/tests/...` has one) |

**Critical structural facts:**

1. **There is NO `conftest.py` anywhere in `tests/`.** (`find` returned only
   `scripts/tests/test_translation_fixer/conftest.py`, unrelated.) This is *the
   single most important harness-enabling fact*: there is no existing
   pytest-collection machinery, fixture tree, or plugin to fight with. We **add
   our own `conftest.py`** at the harness root and it is the *only* conftest →
   total control over what `TestClient` resolves to, with zero conflicts.
2. **Pytest config** lives in `pyproject.toml:224-235`:
   `addopts = ["--strict-config","--strict-markers","--ignore=docs_src"]`,
   `filterwarnings=["error"]` (warnings are errors), `timeout="20"`,
   `strict_xfail=true`, `minversion="9.0"`. The harness must register any custom
   markers (`--strict-markers`) and must not trip `filterwarnings=error` (so the
   shim must not emit a `DeprecationWarning` — note Starlette's TestClient itself
   warns if `timeout=` is passed, `testclient.py:438-443`; FastAPI tests don't
   pass it, so fine).
3. **Two app-acquisition patterns** (both fully compatible with the swap):
   - **Inline app** (134 files): `app = FastAPI(); @app.get(...)` then
     `client = TestClient(app)` at module scope (e.g. `test_application.py:1-7`:
     `from .main import app` → `client = TestClient(app)`).
   - **Tutorial fixture** (249 files in `test_tutorial/`): a pytest fixture
     `importlib.import_module(f"docs_src.{...}")` → `TestClient(mod.app)`
     (canonical shape seen in a sampled `test_tutorial/.../test_*.py:1-30` and
     `test_tutorial/test_body/...`). The app objects come from
     `docs_src/**/*.py` (454 files), each a tiny `app = FastAPI(); @app.<verb>`
     module (e.g. `docs_src/body/tutorial001_py310.py`).
   Both patterns end in `TestClient(<a FastAPI app>)` — so a single swap of what
   `TestClient` *is* covers 100% of both.
4. **`tests/utils.py`** (`:1-30`) provides version-skip markers
   (`needs_py310`, `needs_py314`, `needs_orjson`, `needs_ujson`,
   `workdir_lock = pytest.mark.xdist_group(...)`) and a
   `skip_module_if_py_gte_314()`. The harness must keep `tests/utils.py`
   importable (it's a sibling import: `from ...utils import needs_py310`).
5. **Test dependencies** the harness must install:
   `pytest>=9`, `httpx`, `anyio`, `inline_snapshot` (297 files),
   `dirty_equals` (28 files), `pytest-timeout` (config uses `timeout=`),
   plus `orjson`/`ujson` (optional, gated by markers).

---

## 3. The three classes of FastAPI test (the include/exclude rule)

This is the core decision the brief asks for: which files "just work", which need
a thin adapter, which are FastAPI-internal and must be excluded.

### Class A — Behavioral (RUN UNMODIFIED through the ASGI shim)

**Definition:** the test exercises observable HTTP/WebSocket/lifespan behavior
via `TestClient`. It only touches the public surface (`fastapi.FastAPI`,
`APIRouter`, `Depends`, `Query/Path/Body/Header/Cookie/Form/File`, response
classes, `HTTPException`, `status`) and asserts on `httpx.Response`
(`.status_code`, `.json()`, `.text`, `.headers`).

**Size:** ≈ **540+/581** files. All 434 `fastapi.testclient` importers + the 3
`starlette.testclient` importers + the 92 `docs_src` tutorial drivers fall here
(overlapping sets; net ≈ 95%+).

**Harness action:** **zero edits.** The `conftest.py` swap (§4) makes
`TestClient(app)` → `RealTestClient(app.asgi())`. These pass iff Lancelot is
wire-correct. *This is the entire point — these 540 files ARE the conformance
spec.*

**Sub-lane A2 — snapshot-pinned (xfail-quarantine, still Class A):** the 290
OpenAPI-asserting + many `inline_snapshot` files compare against a *byte-exact*
`inline_snapshot(...)` of FastAPI's OpenAPI JSON / Swagger HTML / ReDoc HTML.
Lancelot generates OpenAPI 3.1 from its *own* compiler (`DESIGN-DECISIONS §3`:
"auto OpenAPI 3.1 ... byte-compatible *where it touches the wire*"). Field
**order**, `$ref` naming, Swagger-UI HTML, and ReDoc HTML will differ
cosmetically even when semantically identical. These go in a **separate parity
lane** (§8): not "fail", but "schema-equivalence" checked by a normalizing
comparator (canonical JSON sort + `$ref` alias map), with raw-byte parity as a
stretch goal. Roughly **180–290 files** touch this; many *also* assert behavior,
so they run in Lane A and only their snapshot assertions are reclassified.

### Class B — Need a thin Lancelot adapter (RUN with a shim object)

**Definition:** the test is behavioral but constructs/inspects a Starlette/FastAPI
*object* the test itself reaches into beyond the HTTP boundary — e.g.
`Request`/`WebSocket` constructed directly in-test, `app.router.routes`
introspection, `app.openapi()` called directly, custom `APIRoute` subclass
(`test_custom_route_class.py`, `test_route_scope.py`), `BackgroundTasks` object
asserted directly.

**Size:** small — **~15–30 files**. Identified seeds (grep this session):
`test_custom_route_class.py`, `test_route_scope.py`,
`test_http_connection_injection.py`, `test_router_events.py` (asserts router
lifespan ordering via a `State` pydantic model — actually behavioral, borderline
A), the 4 `AsyncClient`/`ASGITransport` files, `test_datastructures.py`.

**Harness action:** the harness provides a **compatibility layer module**
`lancelot.compat.fastapi` that re-exports the *public* names FastAPI tests import
(`FastAPI`, `APIRouter`, `Depends`, `Request`, `WebSocket`, `BackgroundTasks`,
`HTTPException`, `status`, `Response` classes) **mapped to Lancelot equivalents
with FastAPI-shaped attributes** (`app.router.routes`, `app.state`,
`app.dependency_overrides`, `app.openapi()`, `route.path`, `route.endpoint`,
`route.methods`). The swap conftest (§4) additionally redirects `import fastapi`
→ this compat module for files in Class B's allowlist. This is a *targeted*
shim, file-allowlisted, **not** a global `fastapi` replacement (a global replace
would also intercept Class C internal imports and mask real divergence).

### Class C — FastAPI-internal, NOT behavioral (EXCLUDE)

**Definition:** the test imports and asserts FastAPI's *private/implementation*
API — the very internals Lancelot deliberately re-implements differently (Rust
core, two-phase Rust-assisted DI compiler). Testing these against Lancelot is
meaningless: they assert *how FastAPI is built*, not *what it does on the wire*.

**Detection rule (grep, deterministic):** any file importing from
`fastapi.dependencies.utils`, `fastapi.dependencies.models`, `fastapi._compat`,
`fastapi.routing` (the module, for internal symbols like `get_request_handler`),
`fastapi.utils`, `fastapi.encoders` (when testing `jsonable_encoder` itself, not
via the wire), `fastapi.params`, `fastapi.concurrency`, or `fastapi.openapi.utils`
**for the purpose of asserting on those internals** (not merely re-exporting).

**Enumerated (this session's grep):**
- `test_dependencies_utils.py` — `from fastapi.dependencies.utils import
  get_typed_annotation` then asserts the function directly (`:1-8`). Pure
  internal. **EXCLUDE.**
- `test_compat.py` — `from fastapi._compat import Undefined,
  is_uploadfile_sequence_annotation` and `from fastapi._compat.shared import
  is_bytes_sequence_annotation` (`:1-12`). Tests FastAPI's Pydantic-v1/v2 compat
  shim — Lancelot has no such shim (Pydantic v2 only, via pydantic-core FFI:
  `DESIGN-DECISIONS §1 fork 4`). **EXCLUDE.**
- `test_jsonable_encoder.py` — tests `jsonable_encoder` directly; Lancelot
  deletes it from the hot path and uses a Rust serializer (`SYNTHESIS §2 #6`).
  The *behavior* it guards (datetime/UUID/Decimal JSON-ization) is re-covered by
  Class-A response tests. **EXCLUDE the direct unit; rely on A coverage.**
- `test_generate_unique_id_function.py` — partially internal (operation-id
  generation); the wire-visible part (operationId in OpenAPI) is Lane A2.
  **EXCLUDE the internal asserts.**
- `test_route_scope.py`, `test_custom_route_class.py` — borderline B/C; route
  *object* introspection → Class B compat layer if cheap, else EXCLUDE.
- `test_multipart_installation.py` — asserts FastAPI's behavior when
  `python-multipart` is absent (an import-error message). Implementation detail
  of FastAPI's optional-dep handling. **EXCLUDE** (Lancelot's multipart story is
  different by design).
- `tests/benchmarks/**` — perf, not conformance. Excluded by FastAPI's own
  `pyproject.toml:248` coverage omit; **EXCLUDE** here too.

**Size:** **~10–18 files.** This is the *only* legitimately-excluded set, and
each exclusion is **justified by a one-line reason in the exclude manifest**
(§5) so "we excluded it to make numbers look good" is impossible — every
exclusion cites the FastAPI-internal symbol it depends on.

**Hard rule (DESIGN):** a file is excluded **only** if it imports a
FastAPI-private module *and* asserts on it. A file that merely imports
`FastAPI` and asserts wire behavior is **never** excludable, regardless of how
hard it is to pass. "Hard to pass" is a Lancelot bug, not an exclusion reason.

---

## 4. Harness directory layout (DESIGN)

Lives in the Lancelot repo (not in the FastAPI checkout — FastAPI's tree is
vendored read-only as a git submodule / pinned tarball so the version is exact
and reproducible).

```
lancelot/
  conformance/
    README.md                      # how to run, how to read parity.json
    pytest.ini                     # harness-side pytest config (markers, lanes)
    conftest.py                    # THE SWAP — rebinds TestClient (§4.1)
    swap.py                        # the ASGI-bridge TestClient wrapper (§4.2)
    compat_fastapi.py              # Class-B public-name shim (§4.3)
    manifests/
      exclude.toml                 # Class-C exclusions, each with a reason (§5)
      xfail_snapshot.toml          # Lane A2 snapshot-pinned files (§5)
      v0_subset.toml               # the v0 must-pass allowlist (§6)
      known_divergence.toml        # accepted, documented behavior deltas
    vendor/
      fastapi/                     # pinned FastAPI checkout, read-only
        tests/                     #   581 files, UNMODIFIED
        docs_src/                  #   454 tutorial app modules, UNMODIFIED
        tests/utils.py             #   kept importable
      robyn/
        integration_tests/         # pinned Robyn integration tests (§7)
    runner/
      run_lane.py                  # invokes pytest per lane, emits JSON
      parity.py                    # aggregates → conformance/parity.json
      ratchet.py                   # CI gate: parity must not regress (§9)
    reports/
      parity.json                  # latest run (committed by CI bot)
      parity-history.ndjson        # one line per CI run, append-only (§8)
  pyproject.toml                   # adds [tool.lancelot.conformance] config
```

**Why vendored, read-only, pinned:** the *only* way "stable like FastAPI" is a
defensible claim is if the test corpus is FastAPI's *exact* released suite at a
pinned tag (e.g. `0.136.x`), byte-unmodified. Modifying a test to make it pass is
the cardinal sin — it converts a measurement into a lie. The pin is recorded in
`vendor/fastapi/FASTAPI_VERSION` and bumped deliberately (a FastAPI upgrade is a
reviewed event, see §9.4).

### 4.1 `conformance/conftest.py` — the swap (DESIGN, reimplementable)

This is the entire mechanism. pytest auto-loads the *nearest* `conftest.py`;
because FastAPI's `tests/` has **none** (KNOWN, §2), ours is authoritative when we
invoke pytest with the harness root on the path and `rootdir` set so this
conftest is collected before the vendored tests.

```python
# conformance/conftest.py
from __future__ import annotations
import os, sys, tomllib, importlib, pathlib
import pytest

HARNESS = pathlib.Path(__file__).parent
VENDOR_FA = HARNESS / "vendor" / "fastapi"

# --- 1. Make the vendored FastAPI tests importable as-is ---------------------
# FastAPI tests do `from .main import app`, `from ...utils import needs_py310`,
# `from docs_src.body... import app`.  We add the vendored roots to sys.path so
# those imports resolve against the pinned tree, unmodified.
sys.path.insert(0, str(VENDOR_FA))            # exposes `docs_src`, `tests`
# (pytest's rootdir/conftest discovery handles the `tests` package itself)

# --- 2. THE SWAP: rebind TestClient before any test module imports it --------
# Every behavioral FastAPI test does `from fastapi.testclient import TestClient`
# at module top.  We replace the symbol at the *module object* level so the
# `from ... import TestClient` name binding resolves to our wrapper.  Done in
# conftest (collected first) => effective for all subsequently-imported tests.
import fastapi.testclient as _fa_tc
from .swap import LancelotASGITestClient
_fa_tc.TestClient = LancelotASGITestClient          # patch the attribute
# also patch the already-imported alias paths some tests use:
import starlette.testclient as _st_tc
sys.modules.setdefault("starlette.testclient", _st_tc)
# (do NOT blanket-replace starlette's, only fastapi's re-export, so Class-B
#  files that explicitly want raw starlette still can.)

# --- 3. Load manifests -------------------------------------------------------
def _load(name):
    p = HARNESS / "manifests" / name
    return tomllib.loads(p.read_text()) if p.exists() else {}

_EXCLUDE  = set(_load("exclude.toml").get("files", []))
_XFAIL    = _load("xfail_snapshot.toml").get("files", {})   # file -> reason
_V0       = set(_load("v0_subset.toml").get("files", []))
_KNOWN    = _load("known_divergence.toml").get("nodeids", {})

LANE = os.environ.get("LANCELOT_LANE", "full")   # "v0" | "full" | "snapshot"

def pytest_ignore_collect(collection_path, config):
    rel = _rel(collection_path)
    if rel in _EXCLUDE:                       # Class C — never collected
        return True
    if LANE == "v0" and rel and rel.endswith(".py") and rel not in _V0:
        return True                           # v0 lane: only the subset
    return None

def pytest_collection_modifyitems(config, items):
    for item in items:
        rel = _rel(item.path)
        # Lane A2: snapshot-pinned files -> xfail(strict=False), tracked apart
        if rel in _XFAIL and ("openapi" in item.name or "snapshot" in rel or
                              "docs" in item.name):
            item.add_marker(pytest.mark.xfail(
                reason=f"snapshot-pinned: {_XFAIL[rel]}", strict=False))
        # documented, accepted divergences -> xfail(strict=True) (must still fail
        # exactly here; if it starts passing, the manifest is stale -> alert)
        if item.nodeid in _KNOWN:
            item.add_marker(pytest.mark.xfail(
                reason=_KNOWN[item.nodeid], strict=True))

def _rel(p):
    try:    return str(pathlib.Path(p).relative_to(VENDOR_FA))
    except Exception: return None
```

Key properties (INFERRED/DESIGN):
- **No test file is edited.** The swap is at the `fastapi.testclient` *module
  attribute*, applied in conftest which pytest imports *before* collecting test
  modules. Each test's `from fastapi.testclient import TestClient` then binds our
  class. (If a future FastAPI test imported the symbol *before* conftest ran,
  the `sys.modules` patch in §4.1 step 2 still covers it — but conftest-first is
  guaranteed by pytest's collection order with `rootdir = conformance/`.)
- **Exclusions are collection-time** (`pytest_ignore_collect`) so excluded files
  never even import — a Class-C file importing `fastapi._compat` symbols that
  don't exist in a Lancelot world can't crash collection.
- **Lanes are env-var driven** (`LANCELOT_LANE`) → one conftest, three CI jobs.

### 4.2 `conformance/swap.py` — the ASGI-bridge TestClient (DESIGN)

The wrapper is *thin* precisely because Starlette's TestClient already does all
the work; we only change *what app it drives*.

```python
# conformance/swap.py
from __future__ import annotations
from typing import Any
from starlette.testclient import TestClient as _StarletteTestClient

def _to_asgi(app: Any):
    """Return a spec-compliant ASGI 3.0 callable for `app`.

    - A Lancelot app: call its design-locked `.asgi()` surface
      (DESIGN-DECISIONS §1 fork 3; design/02 §6.2 seam 3).
    - Already an ASGI callable (raw FastAPI/Starlette app, or a function):
      pass through unchanged — lets Class-B files that build a real
      Starlette app still run, and lets us A/B against real FastAPI.
    """
    if hasattr(app, "asgi") and callable(getattr(app, "asgi")):
        asgi = app.asgi()
        # Stash the originating Lancelot app so `dependency_overrides`,
        # `.state`, lifespan etc. set ON THE TEST'S app object propagate
        # into the ASGI callable's scope["app"] (the §1.3 caveat).
        setattr(asgi, "_lancelot_app", app)
        return asgi
    return app  # raw ASGI app — drive directly (real-FastAPI baseline mode)

class LancelotASGITestClient(_StarletteTestClient):
    """Drop-in for fastapi.testclient.TestClient.

    Identical surface (it IS Starlette's TestClient) — the only change is the
    app is normalized through `.asgi()` first.  Because Starlette's TestClient
    is a pure in-process ASGI driver (testclient.py:199-365, read this
    session), nothing else needs to change: requests/responses are real httpx
    objects, lifespan/websocket/streaming all go through the standard ASGI
    message grammar.
    """
    def __init__(self, app: Any, *args, **kwargs):
        super().__init__(_to_asgi(app), *args, **kwargs)
```

That is the **entire** driver adapter. ~30 lines. It works because §1 proved the
contract is "ASGI 3.0 callable" and Lancelot ships exactly that.

**Why a subclass and not a factory function:** 434 test files do
`client = TestClient(app)` and 14 do `with TestClient(app) as client:`. A
subclass preserves `__enter__/__exit__` (lifespan) and every `.get/.post/.ws`
method verbatim (they're inherited from Starlette → httpx). A factory would have
to re-expose all of `testclient.py:421-667`.

### 4.3 `conformance/compat_fastapi.py` — Class-B public-name shim (DESIGN)

Only loaded for Class-B allowlisted files (via a conftest hook that, for those
file paths, inserts `compat_fastapi` as `sys.modules["fastapi"]` *before* the
test imports). Re-exports the public FastAPI surface mapped onto Lancelot, with
FastAPI-shaped *introspection* attributes the few Class-B tests poke at:

```python
# conformance/compat_fastapi.py  (sketch — surface, not full impl)
from lancelot import App as _LApp
from lancelot import APIRouter, Depends, Query, Path, Body, Header, Cookie
from lancelot import Form, File, UploadFile, HTTPException, status, Request
from lancelot import WebSocket, WebSocketDisconnect, BackgroundTasks
from lancelot.responses import (JSONResponse, PlainTextResponse, HTMLResponse,
                                StreamingResponse, FileResponse, RedirectResponse,
                                Response)

class FastAPI(_LApp):
    """Lancelot App with FastAPI-named introspection attributes Class-B
    tests read (router.routes / .endpoint / .methods / .path, .state,
    .dependency_overrides, .openapi(), .exception_handlers)."""
    # Lancelot's App must expose these as documented public properties;
    # this class only aliases names where they differ. If Lancelot's App
    # already matches FastAPI's public attribute names (a stated v0 goal —
    # DESIGN-DECISIONS §2 "FastAPI-wire-compatibility"), this file is ~empty.
```

If Lancelot honors its own positioning ("FastAPI-wire-compatibility on a Rust
core", `DESIGN-DECISIONS §2`) then `compat_fastapi.py` is *nearly empty* — the
public names already match. The file's existence is insurance for the ~15–30
Class-B introspection edges, not a translation layer.

---

## 5. The include/exclude rule, formalized (DESIGN)

A single deterministic classifier (`runner/classify.py`) tags every one of the
581 files; the tags drive the manifests. The rule, in order:

```
for each tests/**/*.py (and the docs_src apps it drives):
  1. if path under tests/benchmarks/        -> EXCLUDE  (perf, not conformance)
  2. if imports a FASTAPI_PRIVATE module     -> EXCLUDE  (Class C; record symbol)
       FASTAPI_PRIVATE = {
         fastapi.dependencies.utils, fastapi.dependencies.models,
         fastapi._compat, fastapi._compat.*, fastapi.utils,
         fastapi.concurrency, fastapi.openapi.utils, fastapi.params,
         fastapi.encoders (only if it asserts jsonable_encoder directly),
         fastapi.routing (only if it imports private symbols, not APIRouter),
       }
  3. elif asserts a byte-exact OpenAPI / Swagger-HTML / ReDoc-HTML snapshot
        (inline_snapshot of /openapi.json or /docs or /redoc html)
                                              -> LANE A2 (xfail-snapshot)
  4. elif reaches past the HTTP boundary into a Starlette/FastAPI object
        (constructs Request/WebSocket in-test, introspects app.router.routes,
         custom APIRoute subclass)            -> CLASS B (compat shim)
  5. else                                     -> CLASS A (run unmodified)
```

`exclude.toml` example (every line carries its justification — the anti-cheat):

```toml
[files]
"tests/test_dependencies_utils.py"   = "imports fastapi.dependencies.utils.get_typed_annotation; asserts FastAPI-internal reflection helper, not wire behavior"
"tests/test_compat.py"               = "imports fastapi._compat (Pydantic v1/v2 shim); Lancelot is Pydantic-v2-only by design (DESIGN-DECISIONS §1 fork 4)"
"tests/test_jsonable_encoder.py"     = "unit-tests fastapi.encoders.jsonable_encoder directly; deleted from Lancelot hot path (SYNTHESIS §2 #6); behavior recovered by Class-A response tests"
"tests/test_multipart_installation.py" = "asserts FastAPI's import-error string when python-multipart absent; FastAPI implementation detail"
"tests/benchmarks/*"                 = "performance microbenchmarks, not conformance; excluded by FastAPI's own pyproject coverage omit:248"
```

`xfail_snapshot.toml` example:

```toml
[files]
"tests/test_application.py"   = "asserts inline_snapshot of /openapi.json (field order/$ref naming differs; semantic-equiv checked in Lane A2)"
"tests/test_openapi_*.py"     = "byte-exact OpenAPI 3.1 snapshot"
"tests/test_local_docs.py"    = "byte-exact Swagger-UI / ReDoc HTML"
```

**Anti-cheat invariants (DESIGN, enforced by `runner/ratchet.py`):**
- The exclude manifest is **append-only with review**: removing a file from
  EXCLUDE (i.e. starting to count it) is always allowed; *adding* one requires a
  PR-tagged reason matching a `FASTAPI_PRIVATE` import the classifier confirms.
- `runner/classify.py` runs in CI and **fails if `exclude.toml` lists a file
  that does NOT import a private symbol** — you cannot quietly park a hard
  behavioral test in the exclude bin.
- Total counted = `collected − Class C − benchmarks`. Class A2/B are *counted*
  (A2 as a separate %, B as full pass-required).

---

## 6. The v0 must-pass conformance subset (DESIGN, ≈180–220 files)

v0 cannot pass all 581 on day one (OpenAPI byte-snapshots, exotic tutorials).
The **v0 gate is 100% of the table-stakes wire surface** — the set from
`DESIGN-DECISIONS §3`. `v0_subset.toml` enumerates files by the FastAPI test
modules that *are* the spec for each table-stakes feature. Mapping (file globs
→ feature, all Class A, all run unmodified):

| Table-stakes feature (`DESIGN-DECISIONS §3`) | FastAPI test files that ARE its spec |
|---|---|
| Routing, path/query params, methods | `test_application.py`, `test_router*.py`, `test_path.py`, `test_query.py`, `test_extra_routes.py`, `test_include_route*.py`, `test_tutorial/test_path_params/*`, `.../test_query_params/*` |
| Typed body + Pydantic v2 validation | `test_tutorial/test_body*/*`, `test_filter_pydantic_sub_model_pv2.py`, `test_forms_single_model.py`, `test_get_request_body.py`, `test_request_body_parameters_media_type.py` |
| **422 wire-shape** (the ecosystem-load-bearing one) | every file asserting `{"detail":[{type,loc,msg,input,ctx}]}` (233 files grep) — the v0 subset takes the ~40 *canonical* ones: `test_application.py`, `test_path.py`, `test_query.py`, `test_body*`, `test_required_noneable.py`, `test_validate_response*.py` |
| `Depends()` + `dependency_overrides` + yield-teardown | `test_dependency_*.py` (≈30 files: `test_dependency_overrides.py`, `_cache`, `_class`, `_contextmanager`, `_duplicates`, `_after_yield_*`, `_yield_scope*`, `_security_overrides`) — **excluding** the 2 that import `fastapi.dependencies.utils` |
| `include_router` / route groups | `test_include_router_defaults_overrides.py`, `test_router_prefix_with_template_in_path*`, `test_application.py` |
| Lifespan startup/shutdown | `test_router_events.py`, `test_lifespan*`, the 14 `with TestClient` files |
| CORS | `test_tutorial/test_cors/*`, `docs_src/cors/*` |
| sync + async handlers (no 40-thread cliff) | `test_application.py`, sync-handler tutorials; **plus a Lancelot-added load test** (§7 reuse + a new `test_sync_no_cliff.py`) |
| JSON + response model | `test_response_model*.py`, `test_validate_response*.py`, `test_serialize_response*.py` |
| WebSockets | all 11 ws files: `test_ws_router.py`, `test_tutorial/test_websockets/*`, `test_dependency_yield_scope_websockets.py` |
| Static files (ETag/Range/304) | `test_tutorial/test_static_files/*` + `docs_src/static_files/*` |
| Background tasks (detached, fixes FastAPI bug) | `test_tutorial/test_background_tasks/*` — **note:** Lancelot *intentionally diverges* (real detached tasks vs FastAPI's connection-blocking sequential run, `SYNTHESIS §3`); see §6.1 |
| OpenAPI 3.1 *structure* (not byte-exact) | Lane A2 — **not** in the v0 100% gate; v0 requires *semantic* OpenAPI equivalence only |
| OAuth2/security flows | `test_security_*.py`, `test_tutorial/test_security/*` |

**v0 pass-target:** **100% of `v0_subset.toml` (≈180–220 files), 0 xfail in this
lane.** Full-suite (all 581 minus Class C) target at v0 release: **≥ 85%**
(the residual ≈15% is Lane A2 snapshot-cosmetic + long-tail tutorials), with a
**ratchet** (§9) that the number only ever goes up.

### 6.1 Intentional divergences (`known_divergence.toml`, DESIGN)

A handful of FastAPI tests assert FastAPI *bugs* Lancelot deliberately fixes
(`SYNTHESIS §3 AVOID`). These must `xfail(strict=True)` with a cited reason —
they prove Lancelot is *better*, and `strict=True` means if FastAPI's behavior
ever changes such that the test would pass, CI alerts (manifest is stale):

- **`BackgroundTasks` run sequentially before connection release**
  (`SYNTHESIS §3`, `fastapi/01 bonus`): any FastAPI test asserting a background
  task *completed before the response returned* contradicts Lancelot's real
  detached pool. Mark those specific nodeids `xfail(strict=True, reason="Lancelot
  fixes FastAPI's connection-blocking BackgroundTasks — SYNTHESIS §3")`.
- **40-thread sync `CapacityLimiter` cliff**: no FastAPI test asserts the cliff
  (it's an unfixed bug), so nothing to quarantine — but Lancelot *adds* a new
  `conformance/lancelot_extra/test_sync_no_cliff.py` proving 503+Retry-After
  instead of unbounded queueing (a *superset* test, counted in its own lane).

`known_divergence.toml` is the **smallest** manifest and every entry is a
*claimed improvement with a `SYNTHESIS`/`design` citation*. Reviewing this file
is reviewing "where we intentionally aren't FastAPI."

---

## 7. Robyn-test reuse plan (DESIGN)

Robyn's `testing.py` `TestClient` is **NOT reusable as a harness driver**
(KNOWN, `robyn/testing.py:117-350`):

- It is a *handler-direct executor*: `_build()` (`:144-160`) reads
  `app.router.get_routes()` / `app.middleware_router` and calls handlers
  directly; `_execute` (`:217-289`) *re-implements the routing+middleware
  pipeline in Python* ("mirrors `index()` in `src/server.rs`", `:120-124`).
- It returns a bespoke `TestResponse` (`:38-60`), not `httpx.Response`; its
  route matcher is a regex re-implementation (`:70-102`), not the real router.
- It never constructs an HTTP request or ASGI scope — so it tests *the Python
  pipeline mirror*, not the real Rust path. Wrong tool for conformance: it can't
  even catch a Rust-side routing bug because it doesn't use the Rust router.

**What IS reusable: Robyn's integration tests** (`robyn/integration_tests/`,
~30 `test_*.py`) which run against a **real server process**
(`integration_tests/conftest.py:1-80`: `start_server()` spawns
`python3 base_routes.py`, polls `socket.create_connection` until reachable,
session-scoped fixture). These are *real-socket black-box* tests — exactly the
second conformance lane Lancelot wants (validates the actual Rust accept loop /
HTTP/1.1 parser / streaming, which the in-process ASGI lane can't reach).

**Reuse mechanism (DESIGN):**
1. Vendor `robyn/integration_tests/` read-only under
   `conformance/vendor/robyn/`.
2. Provide `conformance/vendor/robyn/base_routes_lancelot.py` — a **Lancelot**
   app re-implementing the same routes `base_routes.py` registers (same paths,
   same expected responses). This is the *only* new code; the test files
   (`test_get_requests.py`, `test_post_requests.py`, `test_sse.py`,
   `test_middlewares.py`, `test_dependency_injection.py`, `test_pydantic.py`,
   `test_websocket*`, `test_status_code.py`, …) run **unmodified** because they
   only do `requests.get(f"http://{BASE_URL}/...")` against a live port.
3. Swap `conftest.py`'s `start_server()` to launch
   `python3 -m lancelot conformance/vendor/robyn/base_routes_lancelot.py`
   instead of `python3 base_routes.py` (one line: the `command` list in
   `conftest.py:start_server`). Everything else (port poll, session fixture,
   `kill_process`) is reused verbatim.
4. This yields a **real-network lane** (`LANCELOT_LANE=robyn_integration`)
   complementing the in-process FastAPI ASGI lane:
   - FastAPI lane = correctness/semantics parity (in-process, fast, 581 files).
   - Robyn-integration lane = real Rust I/O path (socket, parser, SSE,
     graceful-shutdown, concurrency) the ASGI shim structurally cannot exercise.

**Why both lanes are required:** the FastAPI/ASGI lane proves *handler-level
behavior*; it drives `app.asgi()` which (by the dual-tier design) is **not** the
Rust hot path. A const-tier short-circuit, the Rust parser's slow-loris
deadline, SO_REUSEPORT multiprocess, SIGTERM drain — none are observable
in-process. Robyn's integration harness is the ready-made real-socket lane that
covers them, for the cost of *one re-implemented routes file*.

(Robyn's `integration_tests` also has `openapi_config.json`, `test_openapi.py`,
`test_sse.py` — directly reusable to assert Lancelot's SSE framing and OpenAPI
endpoint over a real socket, which is the AI-streaming differentiator's
black-box test.)

---

## 8. Parity tracking over time (DESIGN)

`runner/parity.py` aggregates pytest output (JUnit XML / `pytest-reportlog`)
into a stable schema, one record per lane per run:

```json
// conformance/reports/parity.json  (latest)
{
  "fastapi_version": "0.136.2",
  "lancelot_version": "0.1.0-rc3",
  "timestamp": "2026-05-19T12:00:00Z",
  "git_sha": "abc1234",
  "lanes": {
    "v0_subset":      {"collected": 207, "passed": 207, "failed": 0,
                       "xfail": 0, "skipped": 4, "parity_pct": 100.0},
    "full_behavioral":{"collected": 540, "passed": 481, "failed": 12,
                       "xfail_snapshot": 41, "skipped": 6,
                       "excluded_classC": 14, "parity_pct": 89.07},
    "snapshot_A2":    {"collected": 41, "semantic_equiv": 39, "byte_equal": 7},
    "robyn_integration":{"collected": 30, "passed": 28, "failed": 2}
  }
}
```

- **`parity_pct` per lane** = `passed / (collected − skipped − xfail_snapshot)`
  for behavioral lanes; `semantic_equiv / collected` for A2.
- `parity-history.ndjson` — **append-only, one line per CI run** (sha, ts, every
  lane's pct). This is the "track parity % over time" artifact: a CI step plots
  it (a tiny matplotlib/CSV in `reports/`) so "stable like FastAPI" is a *graph
  trending to 100%*, shown in the README and release notes.
- A failing nodeid is recorded with its assertion diff into
  `reports/failures/<sha>.txt` so regressions are diffable run-to-run.
- **Snapshot semantic-equivalence comparator** (Lane A2): parse both OpenAPI
  docs, canonicalize (`json.dumps(sort_keys=True)` after `$ref` alias-mapping +
  dropping FastAPI-version-string fields), compare. `byte_equal` is the stretch
  metric; `semantic_equiv` is the gate.

---

## 9. The CI gate (DESIGN)

Four parallel CI jobs (one per lane), wired as required status checks:

| Job | Env | Gate (release-blocking) |
|---|---|---|
| `conformance:v0` | `LANCELOT_LANE=v0` | **100% pass, 0 fail, 0 unexpected-xfail.** Hard block on any drop. |
| `conformance:full` | `LANCELOT_LANE=full` | `parity_pct ≥ ratchet_floor` (see §9.1). Block on regression. |
| `conformance:snapshot` | `LANCELOT_LANE=snapshot` | `semantic_equiv ≥ ratchet_floor`. Block on semantic regression (byte advisory). |
| `conformance:robyn` | `LANCELOT_LANE=robyn_integration` | 100% of the reusable subset; new failures block. |

### 9.1 The ratchet (`runner/ratchet.py`, DESIGN)

The mechanism that makes parity monotone (the brief's "track parity % over
time" + "stable like FastAPI" guarantee):

```
ratchet_floor[lane] = max over parity-history.ndjson of parity_pct[lane]
                      observed on the default branch
CI fails if  this_run.parity_pct[lane] < ratchet_floor[lane] - EPS   (EPS=0.0)
On merge to default branch: append this run; ratchet_floor auto-rises.
```

Consequences:
- Parity can **only go up** on the default branch. A PR that drops any lane's
  parity is red until fixed — *this is the literal mechanism of "as stable as
  FastAPI": every commit is provably ≥ the best ever.*
- v0 release criterion (objective, not opinion):
  `v0.parity_pct == 100.0` **and** `full.parity_pct ≥ 85.0` **and**
  `robyn.parity_pct == 100.0` (of reusable subset) **and**
  `snapshot.semantic_equiv ≥ 95.0`.
- "1.0 stable" criterion: `full.parity_pct == 100.0` of all Class A+B, with
  Class C count and every `known_divergence` entry publicly listed in the
  release notes ("here is exactly where and why Lancelot is not FastAPI").

### 9.4 FastAPI version bumps (DESIGN)

`vendor/fastapi/FASTAPI_VERSION` pins the corpus. Bumping it (e.g. 0.136 → 0.137)
is a deliberate PR: re-run the classifier, diff the manifests, re-baseline the
ratchet *only for newly-added files* (existing files' floors never reset). This
keeps "stable like FastAPI" honest *across FastAPI releases* — Lancelot tracks
FastAPI's evolving definition of correct, not a frozen 2026 snapshot.

---

## 10. ASGI-surface obligations the harness imposes on Lancelot (DESIGN→spec feedback)

The harness is also a **spec for `app.asgi()`**. For the unmodified TestClient to
pass, `app.asgi()` MUST (each item = a Starlette TestClient behavior, cited):

1. Be `async def __call__(scope, receive, send)` (ASGI3) — `_is_asgi3`
   detects via `is_async_callable` (`testclient.py:55-58`); a sync callable is
   treated as ASGI2 and wrapped, breaking lifespan. **Must be a true ASGI3
   coroutine callable.**
2. Handle `scope["type"]` ∈ `{"http","websocket","lifespan"}`. Lifespan:
   accept `{"lifespan.startup"}` → emit `lifespan.startup.complete` (or
   `.failed` + message); `{"lifespan.shutdown"}` → `lifespan.shutdown.complete`
   (`testclient.py:708-739`). 14 FastAPI files + Robyn lifespan depend on this.
3. Read request body via `receive()` yielding
   `{"type":"http.request","body":bytes,"more_body":bool}` then
   `{"type":"http.disconnect"}` (`testclient.py:292-318`) — must support
   chunked/streamed bodies (`more_body`) and a post-body disconnect (the
   streaming/SSE tests + `test_stream_cancellation.py` rely on disconnect→cancel,
   which is *also* Lancelot's AI moat — `DESIGN-DECISIONS §3`).
4. Emit exactly one `{"type":"http.response.start","status":int,"headers":
   list[tuple[bytes,bytes]]}` then ≥1 `{"type":"http.response.body","body":
   bytes,"more_body":bool}` (`testclient.py:323-337`). Multiple `start` =
   assertion error in TestClient.
5. Populate `scope["app"]` with a Lancelot app proxy exposing
   `.state`, `.dependency_overrides`, `.router`, `.openapi()`,
   `.exception_handlers` — and **honor the test's mutations to the original app
   object** (FastAPI tests do `app.dependency_overrides[dep]=fake` *after*
   `client = TestClient(app)` and expect it live; `test_dependency_overrides.py`,
   `test_dependency_security_overrides.py`). The `swap.py` `_to_asgi`
   `setattr(asgi,"_lancelot_app",app)` hook (§4.2) is the bridge; `app.asgi()`
   must resolve overrides/state through the *originating app*, not a frozen copy.
6. Honor `scope["state"]` (request state) and `scope["root_path"]`
   (`testclient.py:269-283`) — `behind_a_proxy` tutorials assert `root_path`.
7. WebSocket: accept `websocket.connect` → `websocket.accept`; relay
   `websocket.receive`/`.send`; honor `websocket.close` and the
   `websocket.http.response` denial extension (`testclient.py:142-155, 246-267`)
   — 11 ws files + `test_dependency_yield_scope_websockets.py`.
8. Exception surfacing: when a handler raises and `raise_server_exceptions=True`
   (TestClient default), the exception must propagate out of
   `app(scope,receive,send)` so `portal.call` re-raises it
   (`testclient.py:342-348`) — FastAPI tests do `with pytest.raises(...)`
   around `client.get(...)` expecting the *handler's* exception. Lancelot's
   FFI `catch_unwind` (`DESIGN-DECISIONS §4`) must, in ASGI-surface mode, *not*
   swallow it into a 500 but re-raise (or honor `raise_server_exceptions`).

These 8 items are the precise, testable contract. Each is a line in the
ASGI-surface spec; the conformance harness *is* their executable specification —
which is the whole point: "stable like FastAPI" = "the ASGI surface satisfies
FastAPI's own 74,546-line definition of correct."

---

## 11. Bring-up order (DESIGN — how to reach green fastest)

1. **Skeleton green:** `app.asgi()` returns a trivial ASGI app that 404s
   everything; run the harness; confirm collection works, conftest swap binds,
   manifests load, parity.json emits (expect ~0% — proves the *measurement*
   works before the *framework* does). This de-risks the harness itself.
2. **Hello-world lane:** implement routing + JSON response in `app.asgi()`;
   `test_application.py`'s `test_get_path` passes. First non-zero parity number.
3. **422 wire-shape:** the highest-leverage single feature (233 files assert it).
   Wire pydantic-core validation → exact `{"detail":[...]}`. Expect a large
   parity jump.
4. **`Depends()` + overrides + lifespan** → unlocks ~30 dependency files + the
   14 lifespan files.
5. **WebSockets + static + streaming** → the 11 ws + static + SSE files (also
   lights the Robyn-integration SSE/ws lane).
6. **v0_subset → 100%.** Gate opens. Ship v0.
7. **Snapshot Lane A2 semantic comparator** → push `semantic_equiv` to ≥95%.
8. **Full-suite ratchet** climbs toward 100% release-over-release.

Each step's parity delta is visible in `parity-history.ndjson` — the burn-up
chart *is* the project plan.

---

## 12. Answers to the brief, consolidated

- **Directory layout:** §4 — `lancelot/conformance/` with `conftest.py` (the
  swap), `swap.py` (ASGI-bridge TestClient), `compat_fastapi.py` (Class-B shim),
  `manifests/` (exclude / xfail_snapshot / v0_subset / known_divergence),
  `vendor/` (pinned read-only FastAPI + Robyn), `runner/` (lanes, parity,
  ratchet), `reports/`.
- **Adapter code shape:** §4.1 `conftest.py` (rebind
  `fastapi.testclient.TestClient`, manifest-driven collection/lane logic) + §4.2
  `swap.py` (~30-line `LancelotASGITestClient(_StarletteTestClient)` whose only
  change is `_to_asgi(app)` → `app.asgi()`).
- **Include/exclude rule:** §3, §5 — Class A run unmodified (≈540 files), Class B
  thin compat shim (≈15–30), Class C excluded *only* if importing a
  FASTAPI-private symbol (≈10–18, each with a cited reason; classifier enforces
  the rule so exclusions can't be abused).
- **v0 pass-target:** §6 — **100% of `v0_subset.toml` (≈180–220 files,
  table-stakes wire surface), 0 xfail**; full-suite ≥85% at v0, ratcheting to
  100% for 1.0.
- **Robyn-test reuse plan:** §7 — Robyn's `TestClient` is unusable (handler-direct
  mirror); its **integration tests** are reused as a real-socket lane by
  swapping one line in Robyn's `conftest.py:start_server` to launch a Lancelot
  process serving a re-implemented `base_routes_lancelot.py` (the only new code);
  covers the Rust I/O path the in-process ASGI lane can't.
- **CI gate + parity tracking:** §8, §9 — four lanes, JSON+NDJSON artifacts, a
  monotone **ratchet** so parity can only rise → "as stable as FastAPI" is the
  literal CI invariant, not a claim.
