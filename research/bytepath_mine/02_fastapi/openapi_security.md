# FastAPI — OpenAPI / Security / Exceptions / Middleware shims teardown

**Source cluster:** `fastapi/openapi/` · `fastapi/security/` · `fastapi/exceptions.py` · `fastapi/exception_handlers.py` · `fastapi/middleware/`
**Method:** full read of every file in all four directories; cross-checked against `/Users/seventyleven/Desktop/Lancelot/lancelot/{openapi,security,exceptions,middleware,docs}.py`.
**Additive mandate applied:** items Lancelot already has are tagged HAVE; gaps / improvable items are tagged GAP or IMPROVE.

---

## 1. OpenAPI schema generation (`fastapi/openapi/`)

### 1.1 `constants.py`

```python
METHODS_WITH_BODY = {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"}
REF_PREFIX = "#/components/schemas/"
REF_TEMPLATE = "#/components/schemas/{model}"
```

`METHODS_WITH_BODY` is used at `utils.py:314` to gate request-body generation — `HEAD`, `DELETE` also included, which FastAPI sends a requestBody for. Lancelot's `openapi.py` has the same implicit logic via `request_body_for`. **HAVE.**

### 1.2 `utils.py` — `get_openapi_security_definitions` (`utils.py:81–104`)

Reads `flat_dependant._security_dependencies`; for each, `jsonable_encoder(security_dependency._security_scheme.model, by_alias=True, exclude_none=True)`. Merges scopes per scheme (a scheme can appear in multiple deps with different scopes — scopes are unioned). Result is `(security_definitions_dict, operation_security_list)`.

**Lancelot state:** `openapi.py:_collect_security` (M19b) walks the dep graph transitively and emits the same structure. **HAVE.**

### 1.3 `utils.py` — `separate_input_output_schemas` (`utils.py:514+`)

`get_openapi()` accepts `separate_input_output_schemas: bool = True`. When True, pydantic uses different JSON Schema modes for request validation vs response serialization (e.g. `Optional` fields with defaults differ). This is threaded through every schema generation call via `field_mapping`. 

**Lancelot state:** `App.__init__` accepts `separate_input_output_schemas` as a kwarg but stores it in `_extra_kwargs` (not consumed by `build_openapi`). `openapi.py` always uses pydantic's `TypeAdapter.json_schema` with a single mode. **GAP** — but low priority: 99.6% conformance is already hit with this approach; the failure cases are edge-type annotations that rarely appear in AI/cloud apps. Not a byte-path win; operator DX only.

### 1.4 `utils.py` — JSONL/SSE streaming schema in OpenAPI (`utils.py:357–409`)

FastAPI emits specialized OpenAPI entries for streaming routes:
- `route.is_json_stream == True` → `content["application/jsonl"]["itemSchema"]`
- `route.is_sse_stream == True` → `content["text/event-stream"]["itemSchema"]` with an `_SSE_EVENT_SCHEMA` base + inline `data` contentSchema.

These are derived from `route.stream_item_field` (a `ModelField` set when the handler declares a typed return like `AsyncGenerator[Item, None]`).

**Lancelot state:** `build_openapi` in `openapi.py` does not emit `application/jsonl` or `text/event-stream` itemSchema for `@app.stream`/`@app.batch` routes. The `responses_for()` always produces `application/json`. **GAP** — this is a REAL byte-path-adjacent win: SSE/JSONL streaming is Lancelot's marquee feature, and its OpenAPI description says `application/json`. This misleads SDK codegen (Speakeasy, openapi-typescript, etc.) that auto-generate typed streaming clients. Fix: detect `is_sse_stream`/`is_json_stream` on the route and emit the correct media type + itemSchema. Low code cost; high DX signal for AI streaming adoption.

### 1.5 `utils.py` — `openapi_extra` merge (`utils.py:475–477`)

`deep_dict_update(operation, route.openapi_extra)` — shallow-merge of user-supplied vendor extensions (x-speakeasy-*, x-codeSamples, etc.) LAST (overrides generated keys). Lancelot's `build_openapi` does this too (M15). **HAVE.**

### 1.6 `utils.py` — `validation_error_definition` / `validation_error_response_definition` (`utils.py:42–69`)

Hardcoded inline dicts for the `422` response schema. Lancelot's `openapi.py:_VALIDATION_ERROR` / `_HTTP_VALIDATION_ERROR` are byte-identical copies. **HAVE.**

### 1.7 `utils.py` — webhooks (`utils.py:576–594`)

FastAPI's `get_openapi()` accepts `webhooks: Sequence[BaseRoute] | None` and emits `output["webhooks"]` (OpenAPI 3.1 webhooks object). Uses the same `get_openapi_path()` pipeline.

**Lancelot state:** no webhook path support in `build_openapi`. `App` has `lancelot.webhook` for Standard Webhooks signing/verifying, but that's a runtime primitive, not OpenAPI documentation for outbound webhook shapes. **GAP** — but low priority: operators documenting webhook payloads are a small minority; this is Tier-D adjacent (no byte-path impact; pure DX). Mark as ledgered.

### 1.8 `utils.py` — `get_openapi()` top-level params (`utils.py:514–530`)

Full set: `title`, `version`, `openapi_version="3.1.0"`, `summary`, `description`, `routes`, `webhooks`, `tags`, `servers`, `terms_of_service`, `contact`, `license_info`, `separate_input_output_schemas`, `external_docs`.

**Lancelot state:** `build_openapi(app, title, version)` only takes title + version; summary/description/servers/terms/contact/license/external_docs all drop on the floor. These come from `App(title=, description=, ...)` constructor kwargs — they ARE stored in FastAPI's App and forwarded to `get_openapi`. In Lancelot they land in `_extra_kwargs` and are never forwarded.

**GAP** — `description` and `servers` matter for operator DX: `description` shows in Swagger UI; `servers` is load-bearing for SDK codegen that sets the base URL. Fix: thread `description`/`summary`/`servers`/`tags` from `App` into `build_openapi`. Probably a 10-line change. Medium priority.

### 1.9 `models.py` — Full OpenAPI 3.1 pydantic model hierarchy

All OpenAPI 3.1 model classes (`Schema`, `Operation`, `PathItem`, `Components`, `SecurityScheme`, `OAuthFlows`, etc.) defined with pydantic. FastAPI uses `jsonable_encoder(OpenAPI(**output), by_alias=True, exclude_none=True)` as the final serialization step (`utils.py:606`) — this ensures the output is JSON-compatible Python (dates → strings, etc.) and strips None fields cleanly.

**Lancelot state:** `build_openapi` returns a raw dict, not validated through a pydantic `OpenAPI` model. Works in practice (tests pass at 99.6%) but could emit invalid spec fragments if a path value is not serializable. **IMPROVE** — low priority. The `jsonable_encoder` final pass is a correctness fence. Could add as an optional schema validation pass when `debug=True`.

### 1.10 `docs.py` — `_html_safe_json` + `swagger_ui_parameters` + `init_oauth` + `oauth2_redirect_url`

FastAPI's `get_swagger_ui_html`:
- `_html_safe_json`: replaces `<`, `>`, `&` with `<`/`>`/`&` to prevent injection when embedding JSON in a `<script>` tag (`docs.py:9–19`).
- `swagger_ui_parameters: dict[str, Any]` — merges with `swagger_ui_default_parameters` dict (`docs.py:148–149`).
- `init_oauth: dict` — calls `ui.initOAuth({...})` for pre-populating OAuth2 credentials in Swagger UI (`docs.py:184–187`).
- `oauth2_redirect_url` — sets `oauth2RedirectUrl: window.location.origin + '...'` for the implicit/auth-code flow redirect (`docs.py:174–175`).
- `get_swagger_ui_oauth2_redirect_html()` — the redirect page that Swagger UI pops open for OAuth2 flows (copied from swagger-ui v4.14.0, `docs.py:301–389`).

**Lancelot state:** `lancelot/docs.py:swagger_ui_html` uses a minimal `SwaggerUIBundle({url:..., dom_id:...})` call — no `swagger_ui_parameters` passthrough, no `init_oauth`, no `oauth2_redirect_url`. The `_html_safe_json` escaping uses `json.dumps` (which by default does NOT escape `<`/`>`/`&`). **GAP** — the XSS-safe JSON serialization is a security correctness item: if someone sets `title` or a swagger param to a string containing `</script>`, it breaks the page. The `init_oauth` / `oauth2_redirect_url` support matters only if Lancelot's users do OAuth2 flows via Swagger UI. Medium priority.

---

## 2. Security utilities (`fastapi/security/`)

### 2.1 `base.py` — `SecurityBase` marker

```python
class SecurityBase:
    model: SecurityBaseModel
    scheme_name: str
```

Used as a marker for `isinstance` checks in the DI resolver. **Lancelot state:** `security.py:SecurityBase` is byte-identical. **HAVE.**

### 2.2 `utils.py` — `get_authorization_scheme_param`

```python
def get_authorization_scheme_param(authorization_header_value: str | None) -> tuple[str, str]:
    if not authorization_header_value:
        return "", ""
    scheme, _, param = authorization_header_value.partition(" ")
    return scheme, param.strip()
```

**Lancelot state:** `security.py:_scheme_param` is identical logic. **HAVE.**

### 2.3 `api_key.py` — `APIKeyBase.make_not_authenticated_error` with `WWW-Authenticate: APIKey`

`api_key.py:31–45`: On missing API key, FastAPI sends `WWW-Authenticate: APIKey` (a custom challenge — not in the HTTP spec but RFC 9110 §15.5.2 requires that 401 carries a WWW-Authenticate header). **Lancelot state:** `security.py:_APIKeyBase._check` does exactly this: `{"WWW-Authenticate": "APIKey"}`. **HAVE.**

### 2.4 `http.py` — `HTTPBasic.realm` in WWW-Authenticate

`http.py:197–199`:
```python
def make_authenticate_headers(self) -> dict[str, str]:
    if self.realm:
        return {"WWW-Authenticate": f'Basic realm="{self.realm}"'}
    return {"WWW-Authenticate": "Basic"}
```

**Lancelot state:** `security.py:HTTPBasic._hdrs` does the same. **HAVE.**

### 2.5 `http.py` — `HTTPBase.make_authenticate_headers` uses `scheme.title()`

For `HTTPBase` (the generic HTTP auth base): `{"WWW-Authenticate": f"{self.model.scheme.title()}"}`. This title-cases the scheme name, so a custom `HTTPBase(scheme="custom")` emits `WWW-Authenticate: Custom`. **Lancelot state:** Lancelot does not expose a bare `HTTPBase` class (only the concrete subclasses). Low conformance impact. **HAVE** for the subclasses.

### 2.6 `http.py` — `HTTPDigest` stub

`http.py:319–417`: A stub that validates scheme is `"digest"` but does not implement the full Digest challenge-response. OpenAPI model emits `{"type": "http", "scheme": "digest"}`. The docstring explicitly says "only a stub." **Lancelot state:** Not implemented. **GAP** — but a pure DX/OpenAPI-documentation gap; actual Digest auth is Tier-D (the protocol requires server-side nonce generation, etag, qop — complex, rare in modern AI APIs). Add the stub class for drop-in compatibility, not the protocol.

### 2.7 `oauth2.py` — `OAuth2AuthorizationCodeBearer`

`oauth2.py:547–650`: Adds `authorizationUrl` + `tokenUrl` + `refreshUrl` → `OAuthFlowsModel(authorizationCode=...)`. The `__call__` is identical to `OAuth2PasswordBearer` (checks Authorization: Bearer). **Lancelot state:** Not implemented. **GAP** — common pattern for PKCE/auth-code flows in AI APIs that delegate to an OIDC provider. The actual token exchange is user-side; Lancelot's job is just to verify the Bearer token and emit the correct OpenAPI `securitySchemes` entry.

### 2.8 `oauth2.py` — `OAuth2PasswordRequestFormStrict`

`oauth2.py:162–327`: Subclass of `OAuth2PasswordRequestForm` that makes `grant_type` a REQUIRED field with `Form(pattern="^password$")`. **Lancelot state:** `security.py:OAuth2PasswordRequestForm` exists (permissive form), but `OAuth2PasswordRequestFormStrict` is not present. **GAP** — minor: only matters if a route uses the strict form and the user imports it from `lancelot.security`. No byte-path impact; pure DX conformance.

### 2.9 `open_id_connect_url.py` — `OpenIdConnect`

`open_id_connect_url.py:11–94`: A stub that reads the `Authorization` header and emits `{"type": "openIdConnect", "openIdConnectUrl": "..."}` in the OpenAPI schema. Does NOT fetch the OIDC discovery document or validate JWTs. **Lancelot state:** Not implemented. **GAP** — same level as `HTTPDigest`: pure stub for OpenAPI documentation. Needed for drop-in when a user points Lancelot at an OIDC provider (Keycloak, Okta, Auth0, Cognito) and wants the Swagger UI "Authorize" button to show the correct flow.

### 2.10 `oauth2.py` — `SecurityScopes.scope_str`

`oauth2.py:683–693`: `scope_str = " ".join(self.scopes)` — space-joined scope list for use in WWW-Authenticate headers. **Lancelot state:** `security.py:SecurityScopes.scope_str` is identical. **HAVE.**

---

## 3. Exception handling (`fastapi/exceptions.py`, `fastapi/exception_handlers.py`)

### 3.1 `exceptions.py` — `ValidationException` base with `EndpointContext`

`exceptions.py:10–9`, `174–209`: `ValidationException` stores an `EndpointContext` TypedDict (function, path, file, line) and formats it in `__str__`. Subclasses: `RequestValidationError(body=)`, `WebSocketRequestValidationError`, `ResponseValidationError(body=)`.

**Lancelot state:** `exceptions.py` has all three subclasses. `RequestValidationError` and `ResponseValidationError` have `body=` params. **Missing:** `EndpointContext` TypedDict and the `_format_endpoint_context` / `__str__` formatting that prints file/line in the error message. **GAP** — minor DX: affects developer error messages during development when a handler triggers a 422. No byte-path impact.

### 3.2 `exceptions.py` — `DependencyScopeError`

`exceptions.py:167–171`: `DependencyScopeError(FastAPIError)` — raised at registration time when a function-scope yield-dep depends on a narrower-scope dep. **Lancelot state:** this check and exception exist in Lancelot's DI (the registration-time scope-incompatibility check from M2 conformance push 3). **HAVE** (the check exists; whether the exact class name is exported is less important).

### 3.3 `exception_handlers.py` — `http_exception_handler`

```python
async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    headers = getattr(exc, "headers", None)
    if not is_body_allowed_for_status_code(exc.status_code):
        return Response(status_code=exc.status_code, headers=headers)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)
```

The `is_body_allowed_for_status_code` check means 204/205/304/1xx responses get no body. **Lancelot state:** the ASGI seam handles `HTTPException` this way. The `is_body_allowed_for_status_code` utility is in `lancelot/utils.py`. **HAVE.**

### 3.4 `exception_handlers.py` — `request_validation_exception_handler` uses `jsonable_encoder(exc.errors())`

`exception_handlers.py:20–27`: The 422 body is `{"detail": jsonable_encoder(exc.errors())}` — `jsonable_encoder` converts non-JSON types (e.g. `Enum`, `UUID`, `set`) from pydantic error values into JSON-safe representations. A raw `json.dumps(exc.errors())` would fail on those. **Lancelot state:** the ASGI seam uses the pydantic error list directly; pydantic v2 error dicts are already JSON-safe for standard types. **HAVE** in practice; edge cases with exotic Enum/UUID in field names may diverge.

### 3.5 `exception_handlers.py` — `websocket_request_validation_exception_handler`

`exception_handlers.py:29–34`: On WS validation failure, calls `websocket.close(code=WS_1008_POLICY_VIOLATION, reason=jsonable_encoder(exc.errors()))`. **Lancelot state:** conformance push 2 confirmed WS metaclass + `WS_1008_POLICY_VIOLATION`. **HAVE.**

---

## 4. Middleware shims (`fastapi/middleware/`)

### 4.1 All four FastAPI middleware files are pure re-exports

```python
# cors.py
from starlette.middleware.cors import CORSMiddleware as CORSMiddleware

# gzip.py
from starlette.middleware.gzip import GZipMiddleware as GZipMiddleware

# httpsredirect.py
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware as HTTPSRedirectMiddleware

# trustedhost.py
from starlette.middleware.trustedhost import TrustedHostMiddleware as TrustedHostMiddleware

# wsgi.py
from starlette.middleware.wsgi import WSGIMiddleware as WSGIMiddleware

# asyncexitstack.py (one real class)
class AsyncExitStackMiddleware:
    def __init__(self, app, context_name="fastapi_middleware_astack"):
        ...
    async def __call__(self, scope, receive, send):
        async with AsyncExitStack() as stack:
            scope[self.context_name] = stack
            await self.app(scope, receive, send)
```

FastAPI re-exports Starlette's middleware as `from fastapi.middleware.cors import CORSMiddleware` — a pass-through import alias. The only real implementation is `AsyncExitStackMiddleware` which stores a per-request `AsyncExitStack` in `scope[context_name]`. This is how FastAPI manages yield-dep teardown for file handles, DB connections, etc. that need to outlive the handler but finish before the response is gone.

**Lancelot state:**
- `CORSMiddleware` in `lancelot/middleware.py` is a full re-implementation (not a Starlette re-export), byte-verified at 99.6% conformance. **HAVE.**
- `GZipMiddleware`: M18 shipped `lancelot.compress.GZipMiddleware` (Rust libdeflater off-loop). **HAVE** (and better).
- `HTTPSRedirectMiddleware`, `TrustedHostMiddleware`, `WSGIMiddleware`: not in `lancelot/middleware.py`. **GAP** — low priority. `HTTPSRedirectMiddleware` is trivial (redirect http → https) but redundant when Lancelot is cleartext-behind-a-TLS-proxy (its positioning). `TrustedHostMiddleware` is a useful security shim. `WSGIMiddleware` is Tier-D (mounts a WSGI app — runs synchronously, burns the GIL, contradicts the byte-path mission).
- `AsyncExitStackMiddleware`: not present as a named class. **GAP** — but the yield-dep teardown mechanism in Lancelot uses `AsyncExitStack` internally per-request; it is NOT mounted as ASGI middleware. The FastAPI pattern lets middlewares (non-DI code paths) also access the exit stack via `scope["fastapi_middleware_astack"]`. A niche advanced use case; not needed for conformance.

### 4.2 `AsyncExitStackMiddleware` scope context injection pattern

The key insight: FastAPI mounts `AsyncExitStackMiddleware` unconditionally at app startup (`fastapi/applications.py:build_middleware_stack`) so `scope["fastapi_middleware_astack"]` is ALWAYS available, even in middlewares that run before DI. This is the mechanism that allows `background_tasks` (and other resources) to be reliably closed even when exceptions bubble through middleware. **Lancelot state:** Lancelot's yield-dep teardown uses its own per-request `AsyncExitStack` within the DI seam; not exposed via `scope`. **HAVE** for yield-dep; **GAP** for the scope-exposed pattern. Low priority for Lancelot's use case.

---

## 5. Summary — What to do

### Build (high value, low cost)

1. **SSE/JSONL streaming OpenAPI media type** (`openapi.py`): detect `@app.stream` and emit `text/event-stream` or `application/jsonl` content type + `itemSchema` instead of `application/json`. This is the marquee feature and it shows `application/json` in the docs — misleads every code-gen tool. ~20-30 lines. (`fastapi/openapi/utils.py:357–409`)

2. **`servers`/`description`/`tags` forwarding in `build_openapi`**: thread these from `App._extra_kwargs` into the OpenAPI output dict. `servers` is especially important — SDK codegen uses it for the base URL. ~10 lines. (`fastapi/openapi/utils.py:514–530`, `lancelot/openapi.py:314`)

3. **`OAuth2AuthorizationCodeBearer` stub** (`security.py`): add `authorizationUrl + tokenUrl` OAuthFlows model + Bearer header check. ~20 lines. Critical for apps delegating to OIDC providers (Auth0/Okta/Cognito) — without it, `from lancelot.security import OAuth2AuthorizationCodeBearer` fails. (`fastapi/security/oauth2.py:547–650`)

### Build (medium value, low cost)

4. **`OpenIdConnect` stub** (`security.py`): reads `Authorization`, emits `openIdConnect` scheme in OpenAPI. ~15 lines. (`fastapi/security/open_id_connect_url.py`)

5. **`HTTPDigest` stub** (`security.py`): same shape as `HTTPBearer` but scheme=`digest`. ~10 lines. (`fastapi/security/http.py:319–417`)

6. **`OAuth2PasswordRequestFormStrict`** (`security.py`): subclass with `grant_type` required. ~5 lines. (`fastapi/security/oauth2.py:162–327`)

7. **`_html_safe_json` in `docs.py`**: escape `<`, `>`, `&` in Swagger UI HTML generation. Security correctness, 3 lines. (`fastapi/openapi/docs.py:9–19`)

### Ledger (Tier-D / no byte-path value)

- `separate_input_output_schemas`: conformance edge cases; 99.6% without it.
- `webhooks` in OpenAPI: output webhooks documentation; pure DX; no byte path.
- `WSGIMiddleware`: mounts synchronous WSGI apps; burns GIL; anti-positioning.
- `AsyncExitStackMiddleware` scope injection: advanced pattern; DI seam covers the use case.
- `HTTPSRedirectMiddleware`: trivial redirect; the TLS proxy does this.
- `TrustedHostMiddleware`: useful but minimal security win vs the existing `lancelot.net` SSRF guard.
- `DependencyScopeError` exact class export: purely DX naming; not user-facing in production.
- `EndpointContext` in `ValidationException.__str__`: developer DX; no prod impact.
- Full Digest auth: complex challenge-response protocol; rare in AI/cloud; no byte path.

---

*Mined: 2026-06-22 by subagent (claude-sonnet-4-6). Source sha: fastapi 0.136.1 sdist.*
