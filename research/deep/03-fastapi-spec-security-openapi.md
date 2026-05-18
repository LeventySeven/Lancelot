# FastAPI — Exact Security + OpenAPI 3.1 + Docs Conformance Spec (for byte-identical Lancelot replication)

> Source: `fastapi/fastapi/fastapi/{security,openapi,_compat}/*` + `applications.py`, `utils.py`, `sse.py`,
> `dependencies/{models,utils}.py`, and the `inline_snapshot` OpenAPI tests in `fastapi/fastapi/tests/`.
> This is a **0.136-class fork** (NOT stock PyPI FastAPI): it uses `annotated_doc.Doc`, PEP-604 `str | None`,
> a new `make_not_authenticated_error()` / `make_authenticate_headers()` security layer, `own/parent_oauth_scopes`
> scope plumbing, and `application/jsonl` + `text/event-stream` `itemSchema` streaming OpenAPI. Every divergence
> from stock FastAPI is marked **[FORK]**. Cross-ref `research/design/03` (table-stakes), `fastapi/03` (prior teardown).
> Labels: **KNOWN** (read from source/tests), **INFERRED** (deduced from code paths not directly tested),
> **DESIGN** (Lancelot engineering decision for the Rust+pydantic-core seam).

## Executive summary (12 lines)

1. **Byte-identical OpenAPI is FEASIBLE** but the document is *not* hand-built — it is `jsonable_encoder(OpenAPI(**output), by_alias=True, exclude_none=True)` over a Pydantic model tree, so fidelity = exactly reproducing (a) the assembly order in `get_openapi`/`get_openapi_path`, (b) pydantic-core's `GenerateJsonSchema` JSON-Schema-2020-12 output, (c) `exclude_none` + alias rewrite, (d) `dict` key insertion order (Python dicts are ordered; JSON serialization preserves it).
2. The **hardest part** is (b): the `components.schemas` bodies are produced verbatim by pydantic v2's `GenerateJsonSchema` (a subclassed `bytes_schema` override). Lancelot must call the *same* pydantic-core schema generator via FFI — re-implementing JSON-Schema generation in Rust would drift on edge cases (discriminated unions, `$defs`, computed fields, `anyOf` null ordering, default injection).
3. Security is **8 schemes** (`APIKey{Query,Header,Cookie}`, `HTTPBasic`, `HTTPBearer`, `HTTPDigest`, `HTTPBase`, `OAuth2`, `OAuth2PasswordBearer`, `OAuth2AuthorizationCodeBearer`, `OpenIdConnect`) — each is an `async __call__(request)` returning a value or raising `HTTPException(401)`.
4. **[FORK]** This fork ALWAYS returns **401** (never 403) for failed auth, and ALWAYS attaches a `WWW-Authenticate` header (even API-key, challenge `"APIKey"`). Stock FastAPI returned 403 + no header for HTTP-bearer scheme mismatch. Lancelot must match the **fork**.
5. The 401 body is always `{"detail":"Not authenticated"}` (Starlette `HTTPException` JSON shape).
6. `WWW-Authenticate` values: `Bearer` (OAuth2/OIDC/HTTPBearer), `Basic` or `Basic realm="..."` (HTTPBasic), `<Scheme>.title()` (HTTPBase/Digest → `Digest`), `APIKey` (all 3 APIKey).
7. `auto_error=False` ⇒ return `None` instead of raising (optional auth) — for HTTPBearer/OAuth2*Bearer a *scheme mismatch* also yields `None`.
8. Scope plumbing: `Security(dep, scopes=[...])` accumulates `own_oauth_scopes`; merged into `parent_oauth_scopes` down the DAG; surfaced in OpenAPI as `security: [{SchemeName: [scopes…]}]` and to handlers via `SecurityScopes`.
9. Schema naming: model `__name__` → `normalize_name` (`[^a-zA-Z0-9.\-_]→_`); split into `Name-Input`/`Name-Output` only when `separate_input_output_schemas=True` AND no computed fields differ; `$ref` template `#/components/schemas/{model}`.
10. `components.schemas` keys are emitted **sorted** (`{k: definitions[k] for k in sorted(definitions)}`); body wrapper models named `Body_{operationId}`.
11. Docs: Swagger UI via jsdelivr `swagger-ui-dist@5`, ReDoc `redoc@2`, `/docs` `/redoc` `/openapi.json` `/docs/oauth2-redirect` defaults; HTML built by f-string with `_html_safe_json` XSS escaping (`<`→`<`, `>`→`>`, `&`→`&`).
12. ~63 MUST rules below. Operation IDs: `re.sub(r"\W","_", f"{name}{path_format}") + "_" + method.lower()`.

---

## 1. Security schemes — exact extraction logic, errors, headers

All security classes subclass `fastapi.security.base.SecurityBase` (just `model: SecurityBaseModel; scheme_name: str`).
Each is used as a dependency; FastAPI detects it via `isinstance(unwrapped_call, SecurityBase)` (`dependencies/models.py:91`).
Extraction is `async def __call__(self, request: Request) -> <value> | None`.

### 1.1 The shared header parser — `get_authorization_scheme_param` (`security/utils.py:1-7`) — KNOWN

```python
def get_authorization_scheme_param(authorization_header_value: str | None) -> tuple[str, str]:
    if not authorization_header_value:
        return "", ""
    scheme, _, param = authorization_header_value.partition(" ")
    return scheme, param.strip()
```

**MUST-1**: Authorization parsing is `str.partition(" ")` on the **first space only**; `param` is `.strip()`-ed; missing/empty header → `("", "")`. Scheme is **case-sensitive as returned** but compared via `.lower()` by callers. No multi-space collapsing, no validation of token charset.

### 1.2 APIKey family (`security/api_key.py`) — KNOWN

`APIKeyBase.__init__(location: APIKeyIn, name, description, scheme_name, auto_error)` builds
`self.model = APIKey(**{"in": location}, name=name, description=description)` and
`self.scheme_name = scheme_name or self.__class__.__name__` (`api_key.py:22-29`).

**[FORK] MUST-2** — `make_not_authenticated_error()` (`api_key.py:31-45`):
```python
HTTPException(status_code=401, detail="Not authenticated",
              headers={"WWW-Authenticate": "APIKey"})
```
The `WWW-Authenticate: APIKey` challenge for API keys is fork-specific (stock FastAPI raised 403 with no header). **Lancelot must emit `APIKey`.**

**MUST-3** — `check_api_key(api_key)` (`api_key.py:47-52`): if falsy → `auto_error` ? raise the 401 : return `None`; else return the key string unchanged.

Extraction per subclass:
- `APIKeyQuery.__call__`: `request.query_params.get(self.model.name)` (`api_key.py:142-144`).
- `APIKeyHeader.__call__`: `request.headers.get(self.model.name)` (`api_key.py:230-232`) — case-insensitive header lookup (Starlette `Headers`).
- `APIKeyCookie.__call__`: `request.cookies.get(self.model.name)` (`api_key.py:318-320`).

**MUST-4**: empty-string value counts as missing (`if not api_key`). A present-but-empty query param `?key=` ⇒ 401 (under auto_error).

OpenAPI model objects (verbatim, tests `test_security_api_key_{header,query,cookie}.py`):
- header: `{"type": "apiKey", "name": "key", "in": "header"}`
- query: `{"type": "apiKey", "name": "key", "in": "query"}`
- cookie: `{"type": "apiKey", "name": "key", "in": "cookie"}`
- with description: adds `"description": "An API Key Header"` (key order: `type, in, name, description` after alias+exclude_none — see §3.4).

### 1.3 HTTP family (`security/http.py`) — KNOWN

`HTTPBase` base (`http.py:69-102`):
- `model = HTTPBaseModel(scheme=scheme, description=description)`; `scheme_name = scheme_name or cls.__name__`.
- **[FORK]** `make_authenticate_headers()` returns `{"WWW-Authenticate": f"{self.model.scheme.title()}"}` (`http.py:84-85`). `.title()` ⇒ `"digest"`→`"Digest"`, `"bearer"`→`"Bearer"`.
- `make_not_authenticated_error()` → `HTTPException(401, "Not authenticated", headers=make_authenticate_headers())` (`http.py:87-92`).
- `__call__`: parse `Authorization`; if `not (authorization and scheme and credentials)` → auto_error ? raise : `None`; else `HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)` (`http.py:94-102`).

**HTTPBasic** (`http.py:105-219`):
- `model = HTTPBaseModel(scheme="basic", ...)`; extra ctor arg `realm`.
- **MUST-5** `make_authenticate_headers()`: `{"WWW-Authenticate": f'Basic realm="{self.realm}"'}` if `realm` else `{"WWW-Authenticate": "Basic"}` (`http.py:197-200`). Test `test_security_http_basic_realm.py:31` ⇒ `Basic realm="simple"`.
- **MUST-6** `__call__` (`http.py:202-219`): if `not authorization or scheme.lower() != "basic"` → auto_error ? raise : `None`. Then `b64decode(param).decode("ascii")`; on `ValueError|UnicodeDecodeError|binascii.Error` → raise the 401 (`from e`). Split `data.partition(":")`; if no separator → raise. Return `HTTPBasicCredentials(username, password)`. Username may be empty; password may be empty; everything after first `:` is password.

**HTTPBearer** (`http.py:222-316`):
- `model = HTTPBearerModel(bearerFormat=bearerFormat, description=description)`; OpenAPI scheme fixed `"bearer"`, optional `bearerFormat`.
- **[FORK] MUST-7** `__call__` (`http.py:303-316`): if `not (authorization and scheme and credentials)` → auto_error ? raise(401 Bearer) : `None`. **Then a SECOND check**: `if scheme.lower() != "bearer"` → auto_error ? raise(401 Bearer) : `None`. Test `test_security_http_bearer.py:32-36`: `Authorization: Basic notreally` ⇒ **401** `{"detail":"Not authenticated"}` + `WWW-Authenticate: Bearer`. (Stock FastAPI returned 403 here — Lancelot must match the fork's **401**.)

**HTTPDigest** (`http.py:319-417`):
- `model = HTTPBaseModel(scheme="digest", ...)`. Same two-step pattern as Bearer but `scheme.lower() != "digest"`. `WWW-Authenticate: Digest` (`.title()` of `"digest"`). Test `test_security_http_digest.py:29` ⇒ `Digest`. It is a stub (returns raw `HTTPAuthorizationCredentials`; does not implement digest challenge/nonce).

OpenAPI security objects: `{"type": "http", "scheme": "basic"}`, `{"type": "http", "scheme": "bearer"}` (bearer never emits `bearerFormat` unless set; `exclude_none` drops it), `{"type": "http", "scheme": "digest"}`.

### 1.4 OAuth2 family (`security/oauth2.py`) — KNOWN

`OAuth2` base (`oauth2.py:330-430`):
- `model = OAuth2Model(flows=cast(OAuthFlowsModel, flows), description=description)`; default `flows=OAuthFlowsModel()` (empty).
- **MUST-8** `make_not_authenticated_error()` → `HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})` (`oauth2.py:401-421`).
- **MUST-9** base `__call__` (`oauth2.py:423-430`): `authorization = request.headers.get("Authorization")`; if `not authorization` → auto_error ? raise : `None`; else **return the raw full header string** (NOT just the token). Test `test_security_oauth2.py:46-54`: `Bearer footokenbar` ⇒ value `"Bearer footokenbar"`; `Other footokenbar` ⇒ value `"Other footokenbar"` (200 — base OAuth2 does not validate scheme).

`OAuth2PasswordBearer.__call__` (`oauth2.py:536-544`): parse; if `not authorization or scheme.lower() != "bearer"` → auto_error ? raise(401 Bearer) : `None`; else return **`param`** (token only, no scheme).

`OAuth2AuthorizationCodeBearer.__call__` (`oauth2.py:642-650`): identical to PasswordBearer (return `param`); the non-auto-error branch is `# pragma: nocover`.

**Flow models** built in subclass ctors:
- `OAuth2PasswordBearer`: `OAuthFlowsModel(password={"tokenUrl":…, "refreshUrl":…, "scopes":scopes or {}})` (`oauth2.py:517-528`).
- `OAuth2AuthorizationCodeBearer`: `OAuthFlowsModel(authorizationCode={"authorizationUrl":…, "tokenUrl":…, "refreshUrl":…, "scopes":scopes or {}})` (`oauth2.py:622-634`).
- **MUST-10** `refreshUrl=None` is dropped by `exclude_none`; empty `scopes` defaults to `{}` and **is emitted** (`OAuthFlow.scopes: dict[str,str] = {}` is not None). Test `test_security_oauth2.py:264-277`: scheme object is
  `{"type":"oauth2","flows":{"password":{"scopes":{...},"tokenUrl":"token"}}}` — note key order `scopes` then `tokenUrl` (model field order in `OAuthFlow`→`OAuthFlowPassword`: `refreshUrl`(dropped), `scopes`, then `tokenUrl`).

`OAuth2PasswordRequestForm` / `…Strict` (`oauth2.py:14-327`) are **form dependency classes** (not security schemes): fields `grant_type`(`Form(pattern="^password$")`, optional in non-strict / required+`Annotated[str, Form(...)]` in strict), `username`(`Form()` required), `password`(`Form(json_schema_extra={"format":"password"})`), `scope`(`Form()` default `""`, `.split()` → `self.scopes` list), `client_id`(opt), `client_secret`(opt, `format:password` in non-strict only). Test `test_security_oauth2.py:64-146` shows the 422 shape for missing/mismatched `grant_type` (`string_pattern_mismatch`, `ctx:{"pattern":"^password$"}`).

`SecurityScopes` (`oauth2.py:653-693`): `scopes: list[str]`, `scope_str = " ".join(self.scopes)`. Injected by the DI solver (§2.3).

### 1.5 OpenIdConnect (`security/open_id_connect_url.py`) — KNOWN

- `model = OpenIdConnectModel(openIdConnectUrl=…, description=…)` (`open_id_connect_url.py:74-76`).
- `make_not_authenticated_error()` → 401 + `WWW-Authenticate: Bearer` (`:80-85`).
- `__call__` (`:87-94`): `request.headers.get("Authorization")`; if missing → auto_error ? raise : `None`; else **return raw full header** (like base OAuth2). Test `test_security_openid_connect.py:31-45`: `Bearer footokenbar`⇒`"Bearer footokenbar"`; no header ⇒ 401 + `WWW-Authenticate: Bearer`.
- OpenAPI: `{"type":"openIdConnect","openIdConnectUrl":"/openid"}`.

### 1.6 Conformance matrix — auth behavior (the table Lancelot must satisfy bit-for-bit)

| Scheme | Missing/empty creds (auto_error=True) | Status | `WWW-Authenticate` | auto_error=False | Returns on success |
|---|---|---|---|---|---|
| APIKeyQuery/Header/Cookie | `not api_key` | **401** | `APIKey` | `None` | key str (as-is) |
| HTTPBase | not (auth & scheme & creds) | **401** | `<scheme>.title()` | `None` | `HTTPAuthorizationCredentials(scheme,credentials)` |
| HTTPBasic | no auth OR scheme≠basic; bad b64; no `:` | **401** | `Basic` or `Basic realm="R"` | `None` (only the no-auth/scheme path) | `HTTPBasicCredentials(username,password)` |
| HTTPBearer | not(auth&scheme&creds) OR scheme≠bearer | **401** | `Bearer` | `None` (both paths) | `HTTPAuthorizationCredentials("Bearer",token)` |
| HTTPDigest | not(auth&scheme&creds) OR scheme≠digest | **401** | `Digest` | `None` (both paths) | `HTTPAuthorizationCredentials("Digest",creds)` |
| OAuth2 (base) | `not authorization` | **401** | `Bearer` | `None` | **raw full header string** |
| OAuth2PasswordBearer | no auth OR scheme≠bearer | **401** | `Bearer` | `None` | token (`param`) |
| OAuth2AuthorizationCodeBearer | no auth OR scheme≠bearer | **401** | `Bearer` | `None` (nocover) | token (`param`) |
| OpenIdConnect | `not authorization` | **401** | `Bearer` | `None` | **raw full header string** |

**MUST-11**: Body for every auth failure = Starlette `HTTPException` → `JSONResponse({"detail":"Not authenticated"}, status_code=401)`. Header casing: `WWW-Authenticate` (HTTP-title-case as written in the dict; Lancelot's writer must preserve `WWW-Authenticate`).

**MUST-12**: `auto_error=False` for HTTPBasic only short-circuits the *no-auth/scheme-mismatch* branch; a malformed base64 or missing `:` **still raises 401** even when `auto_error=False` (`http.py:212-218` has no auto_error guard on the decode path). Lancelot must reproduce this asymmetry.

---

## 2. Scope checking — collection, NOT enforcement (the subtle part)

**MUST-13 — FastAPI does NOT enforce OAuth2 scopes.** Critical: the security `__call__` is invoked exactly like
any other dependency (`dependencies/utils.py:677-680`: `is_coroutine_callable` ⇒ `await call(**values)`, else
`run_in_threadpool`). There is **no scope comparison anywhere in the framework**. Scopes are only *propagated*
into a `SecurityScopes` object the user's dependency reads to do its own `if scope not in security_scopes.scopes: raise HTTPException(403,…)`.
Lancelot replicates the *plumbing*, not an enforcement engine.

### 2.1 Scope accumulation in the DI DAG (`dependencies/models.py:53-71`, `dependencies/utils.py:305,330-342`) — KNOWN

- `Dependant.own_oauth_scopes` = scopes from the `Security(dep, scopes=[...])` that *introduced* this dependant
  (`utils.py:331-333`: only `params.Security` carries `.scopes`; `params.Depends` does not).
- `Dependant.parent_oauth_scopes` = all scopes accumulated from ancestors (`current_scopes = (parent or []) + (own or [])`, `utils.py:305`; passed as `parent_oauth_scopes=current_scopes` to each sub-dependant, `utils.py:339`).
- `@cached_property oauth_scopes` (`models.py:53-60`): `parent.copy()` then append each `own` scope **not already present** (order-preserving, dedup but no sort).

**MUST-14** — scope merge for OpenAPI uses a different shape than for caching:
- OpenAPI (`get_openapi_security_definitions`, `openapi/utils.py:96-103`): per `security_name`, iterate `security_dependency.oauth_scopes`, append scope if `scope not in list` → `operation_security = [{name: scopes}]`. **Order = first-seen order, deduped, NOT sorted** (test `test_security_oauth2_authorization_code_bearer_scopes_openapi.py:131` ⇒ `["read","write"]` in declaration order).
- Cache key (`models.py:62-71`): `(self.call, tuple(sorted(set(oauth_scopes))) if _uses_scopes else (), computed_scope or "")` — here scopes **are sorted+setted** (cache identity), distinct from the OpenAPI ordering. Lancelot's plan-compiler must keep these two scope representations separate.

`get_parameterless_sub_dependant` (`utils.py:123-135`): `dependencies=[Security(dep, scopes=[…])]` (parameterless) ⇒ `own_oauth_scopes = list(depends.scopes)` when `isinstance(depends, params.Security)`.

`get_flat_dependant` (`utils.py:138-189`): flattens the DAG, threading `parent_oauth_scopes=flat_dependant.oauth_scopes` into children, with `skip_repeats` keyed on `cache_key`. The flat dependant's `_security_dependencies` (`models.py:100-103`) = sub-deps where `_is_security_scheme` (i.e. `isinstance(_unwrapped_call(call), SecurityBase)`).

### 2.2 `SecurityScopes` injection (`dependencies/utils.py:725-728`) — KNOWN

```python
if dependant.security_scopes_param_name:
    values[dependant.security_scopes_param_name] = SecurityScopes(scopes=dependant.oauth_scopes)
```
**MUST-15**: any dependency parameter annotated `SecurityScopes` (detected at `utils.py:380-381` via `lenient_issubclass(type_annotation, SecurityScopes)`) receives a fresh `SecurityScopes(scopes=<this dependant's accumulated oauth_scopes>)`. `.scope_str = " ".join(scopes)`. The scopes a given dependency sees = the union of all `Security(...scopes=...)` on the path from the route down to (and including) itself.

### 2.3 Dependency-cache interaction with scopes — KNOWN

`cache_key` (`models.py:62-71`) includes scopes only if `_uses_scopes` (own scopes set, OR has a `SecurityScopes` param, OR is itself a security scheme, OR any sub-dep uses scopes — `models.py:73-84`). **MUST-16**: a security dependency called twice in one request with the *same* sorted scope set is solved once (cached); with *different* scope sets it is solved twice. Test `test_security_scopes.py:39-45`: `get_db` (no scopes) called once even though reached via both a plain `Depends` and a `Security(..., scopes=["read"])` chain — because `get_db` itself does not use scopes, its cache key ignores scopes.

---

## 3. The exact OpenAPI 3.1 document — assembly algorithm + structure

`get_openapi` (`openapi/utils.py:514-606`) is the single entry. Output is **`jsonable_encoder(OpenAPI(**output), by_alias=True, exclude_none=True)`** (`utils.py:606`). Byte-fidelity = reproduce this assembly + the Pydantic model alias/exclude pass + key order.

### 3.1 Top-level `get_openapi` order (`openapi/utils.py:514-606`) — KNOWN

1. `info = {"title": title, "version": version}`; then conditionally add (in this order) `summary`, `description`, `termsOfService`, `contact`, `license` (`utils.py:531-541`).
2. `output = {"openapi": openapi_version, "info": info}` (`openapi_version` default `"3.1.0"`, `applications.py:921`).
3. if `servers`: `output["servers"] = servers` (`utils.py:543-544`).
4. `all_fields = get_fields_from_routes(routes + webhooks)` — order: callback flat models, then `body_fields + responses + request_fields` (`utils.py:481-511`).
5. `flat_models = get_flat_models_from_fields(all_fields, set())`; `model_name_map = get_model_name_map(flat_models)`; `field_mapping, definitions = get_definitions(...)` (§3.3).
6. For each `APIRoute` in `routes`: `get_openapi_path(...)` → `paths.setdefault(route.path_format, {}).update(path)`; merge `securitySchemes`, `definitions` (`utils.py:557-575`).
7. Same loop for `webhooks` → `webhook_paths` (`utils.py:576-594`).
8. **MUST-17** if `definitions`: `components["schemas"] = {k: definitions[k] for k in sorted(definitions)}` — schema keys are **lexicographically sorted** (`utils.py:596`). `securitySchemes` are inserted in route-iteration order (NOT sorted).
9. `output` key order: `openapi, info, [servers], [components], paths, [webhooks], [tags], [externalDocs]` (`utils.py:542-606`). Note: `components` is added **before** `paths` in the dict, so JSON emits `components` before `paths` *unless* the `OpenAPI` Pydantic model reorders — see §3.5.
10. `return jsonable_encoder(OpenAPI(**output), by_alias=True, exclude_none=True)`.

### 3.2 Per-operation `get_openapi_path` (`openapi/utils.py:260-478`) — KNOWN

Builds `path` dict, `security_schemes`, `definitions`. Per HTTP method (only if `route.include_in_schema`):

- `operation = get_openapi_operation_metadata(...)` (`utils.py:236-257`): key order `tags`(if any), `summary`, `description`(if any), `operationId`, `deprecated`(if truthy). **MUST-18** `summary = route.summary or route.name.replace("_"," ").title()` (`utils.py:230-233`). `operationId = route.operation_id or route.unique_id`; duplicate operationId ⇒ `warnings.warn` but still emitted (`utils.py:245-254`).
- Security (`utils.py:286-293`): `flat_dependant = get_flat_dependant(route.dependant, skip_repeats=True)`; `security_definitions, operation_security = get_openapi_security_definitions(flat_dependant)`. If `operation_security`: `operation.setdefault("security",[]).extend(operation_security)`. `security_schemes.update(security_definitions)`.
- `get_openapi_security_definitions` (`utils.py:81-104`): for each `flat_dependant._security_dependencies`:
  `security_definition = jsonable_encoder(dep._security_scheme.model, by_alias=True, exclude_none=True)`;
  `security_definitions[dep._security_scheme.scheme_name] = security_definition`; merge `oauth_scopes` (dedup, first-seen order) into `operation_security_dict[name]`; `operation_security = [{name:scopes} for …]`.
- Parameters (`utils.py:294-313`): `_get_openapi_operation_parameters(...)`; dedup via `{(in,name):param}`, then required overrides non-required (`all_parameters.update(required_parameters)`), `operation["parameters"] = list(all_parameters.values())`.
- requestBody (`utils.py:314-322`): only if `method in METHODS_WITH_BODY = {"GET","HEAD","POST","PUT","DELETE","PATCH"}`; `get_openapi_operation_request_body(...)`.
- callbacks (`utils.py:323-339`): recursive `get_openapi_path` per callback → `operation["callbacks"][cb.name] = {cb.path: cb_path}`.
- **MUST-19** status code (`utils.py:340-355`): `str(route.status_code)` if set, else introspect `current_response_class.__init__` signature's `status_code` default int. `operation.setdefault("responses",{}).setdefault(status_code,{})["description"] = route.response_description` (default `"Successful Response"`).
- Response content (`utils.py:356-409`), only if `is_body_allowed_for_status_code(route.status_code)` (`utils.py` helper: False for `<200`, `204`, `205`, `304`; True for `None`/`default`/`NXX`):
  - **[FORK]** if `route.is_json_stream`: `responses[sc].content["application/jsonl"] = {"itemSchema": <stream_item schema or {}>}`.
  - **[FORK]** elif `route.is_sse_stream`: deep-copy `_SSE_EVENT_SCHEMA` (`sse.py:9`: `{"type":"object","properties":{"data":{"type":"string"},"event":{"type":"string"},"id":{"type":"string"},"retry":{"type":"integer","minimum":0}}}`); if `stream_item_field`: set `required:["data"]` and `properties.data = {"type":"string","contentMediaType":"application/json","contentSchema":<schema>}`. `responses[sc].content["text/event-stream"] = {"itemSchema": item_schema}`.
  - elif `route_response_media_type`: `response_schema = {"type":"string"}`; if `JSONResponse` subclass: schema = `get_schema_from_model_field(route.response_field)` or `{}` if no response_field. Set `responses[sc].content[media_type]["schema"]`.
- `route.responses` (additional, `utils.py:410-453`): per `additional_status_code`: deep-copy, `pop("model")`, key uppercased (`"DEFAULT"`→`"default"`), `deep_dict_update`, description = `process_response.description or openapi_response.description or status_text or "Additional Response"` where `status_text = status_code_ranges.get(...) or http.client.responses.get(int(...))`.
- **MUST-20** 422 injection (`utils.py:454-474`): `if (all_route_params or route.body_field) and not any(s in operation["responses"] for s in ["422","4XX","default"])`: add `operation["responses"]["422"] = {"description":"Validation Error","content":{"application/json":{"schema":{"$ref":"#/components/schemas/HTTPValidationError"}}}}`; and if `"ValidationError" not in definitions`: add `validation_error_definition` + `validation_error_response_definition` (verbatim consts at `utils.py:42-69`).
- `route.openapi_extra` (`utils.py:475-476`): `deep_dict_update(operation, route.openapi_extra)`.
- `path[method.lower()] = operation` (`utils.py:477`).

### 3.3 `get_definitions` — the `$defs` builder + Input/Output split (`_compat/v2.py:285-346`) — KNOWN

This is **the byte-fidelity crux**. The schema bodies are produced by pydantic v2's `GenerateJsonSchema`, NOT by FastAPI:

```python
schema_generator = GenerateJsonSchema(ref_template=REF_TEMPLATE)   # REF_TEMPLATE = "#/components/schemas/{model}"
# split fields by mode
validation_fields   = [f for f in fields if f.mode == "validation"]
serialization_fields = [f for f in fields if f.mode == "serialization"]
# flat models per mode → ModelField(FieldInfo(annotation=model), name=model.__name__, mode=…)
# dedup vs input_types = {f.field_info.annotation for f in fields}
inputs = [(field, (field.mode if (separate_input_output_schemas or _has_computed_fields(field)) else "validation"),
           field._type_adapter.core_schema) for field in list(fields)+list(unique_flat_model_fields)]
field_mapping, definitions = schema_generator.generate_definitions(inputs=inputs)
for item_def in definitions.values():
    if "description" in item_def:
        item_def["description"] = item_def["description"].split("\f")[0]   # truncate at form-feed
return field_mapping, definitions
```

**MUST-21** — `REF_TEMPLATE = "#/components/schemas/{model}"`, `REF_PREFIX = "#/components/schemas/"` (`openapi/constants.py`). Every `$ref` uses this exact prefix.

**MUST-22 — Input/Output split**: pydantic's `generate_definitions` emits `Name-Input` and `Name-Output` suffixes when the same model is used in both `mode="validation"` and `mode="serialization"` AND they differ AND `separate_input_output_schemas=True`. If `separate_input_output_schemas=False` OR the field has computed fields equal across modes, `override_mode="validation"` collapses to a single `Name`. The `-Input`/`-Output` suffix is **pydantic-core's behavior**, not FastAPI's — Lancelot MUST call the same pydantic `GenerateJsonSchema(ref_template=...)` to get identical suffixing and `$defs` collision resolution (pydantic appends `-Input`/`-Output`/numeric suffixes deterministically). Test `test_openapi_separate_input_output_schemas.py:326-416`: `Item-Input` (required `["name"]`) vs `Item-Output` (required `["name","description","sub"]` because `json_schema_serialization_defaults_required:True`), `WithComputedField-Output` adds `computed_field` with `readOnly:True`.

**MUST-23 — description form-feed truncation**: any model docstring/description is truncated at the first `\f` (form-feed) char (`v2.py:339`). Test `test_openapi_model_description_trim_on_formfeed.py` covers this.

**MUST-24 — single-model = `$ref`-only** (no Input/Output suffix when used only as response and not separated): test `test_duplicate_models_openapi.py:64-81` ⇒ bare `Model`, `Model2`, `Model3` (no suffix) when only response-side and shared structurally.

**MUST-25 — bytes schema override** (`GenerateJsonSchema.bytes_schema`, `v2.py:60-70`): `bytes` → `{"type":"string","contentMediaType":"application/octet-stream"}`, plus `"contentEncoding":"base64"` when `ser/val_json_bytes == "base64"`. This is a FastAPI subclass override of pydantic's default — Lancelot must apply the same override on top of pydantic-core.

### 3.4 `get_schema_from_model_field` + model naming (`_compat/v2.py:254-282, 425-434`) — KNOWN

- `override_mode = None if (separate_input_output_schemas or _has_computed_fields(field)) else "validation"`.
- `field_alias = (validation_alias or alias) if mode=="validation" else (serialization_alias or alias)`.
- `json_schema = field_mapping[(field, override_mode or field.mode)]`.
- **MUST-26** if `"$ref" not in json_schema`: inject `json_schema["title"] = field.field_info.title or field_alias.title().replace("_"," ")` — i.e. an inline (non-`$ref`) param/response schema always gets a `title` = field title or `Alias.title()` with underscores→spaces. `$ref` schemas get NO injected title (the `$ref` stands alone). Test `test_openapi_separate_input_output_schemas.py:182`: list-response gets `"title":"Response Read Items Items  Get"` (double-space from `__` in path → see operationId).
- **MUST-27** `normalize_name(name) = re.sub(r"[^a-zA-Z0-9.\-_]", "_", name)` (`v2.py:425-426`); `get_model_name_map` maps each unique model's `__name__`→normalized name (last-wins on collision via dict round-trip `{v:k for k,v in name_model_map.items()}`, `v2.py:429-434`).

### 3.5 The final serialization pass — `jsonable_encoder(OpenAPI(**output), by_alias=True, exclude_none=True)` — KNOWN

**MUST-28 — KEY ORDER IS THE PYDANTIC MODEL'S FIELD ORDER, NOT THE BUILD-DICT ORDER.** `get_openapi` builds a plain dict, but the last line wraps it in the `OpenAPI` Pydantic model and calls `jsonable_encoder`, which (`encoders.py:243-258`) does `obj.model_dump(mode="json", by_alias=True, exclude_none=True, ...)`. **`model_dump` emits keys in model field-declaration order.** So the wire order is fixed by `openapi/models.py` class definitions:
- `OpenAPI` (`models.py:419-431`): `openapi, info, jsonSchemaDialect, servers, paths, webhooks, components, security, tags, externalDocs`. **⇒ `paths` is emitted BEFORE `components`** on the wire (the build dict had `components` first — irrelevant). Test snapshots confirm: `openapi, info, [externalDocs is set via output and re-ordered to end], paths, components`.
- `Operation` (`models.py:289-302`): `tags, summary, description, externalDocs, operationId, parameters, requestBody, responses, callbacks, deprecated, security, servers`. Test `test_security_oauth2.py:158` shows `responses, summary, operationId, requestBody` — wait: the snapshot order is `responses` then `summary` then `operationId`. **[CRITICAL]** This means the snapshots are compared as **dicts (order-insensitive in `==`)**, BUT `json.dumps` of `model_dump` will follow `Operation` field order: `summary, operationId, [requestBody], responses, [security]`. Lancelot MUST emit in **Pydantic model field order**, and to verify byte-fidelity must compare serialized JSON text, not Python dict equality (the FastAPI tests use dict `==`, which hides ordering — Lancelot's conformance harness must dump+compare strings).
- `Info`: `title, summary, description, termsOfService, contact, license, version` (`models.py:73-80`) — note `version` is LAST in `Info`. Test snapshots `{"title":"FastAPI","version":"0.1.0"}` (only 2 set; `version` after `title`).
- `Parameter`: `description, required, deprecated, style, explode, allowReserved, schema_(alias schema), example, examples, content, name, in_(alias in)` (`models.py:243-261`).
- `Components`: `schemas, responses, parameters, examples, requestBodies, headers, securitySchemes, links, callbacks, pathItems` (`models.py:399-410`) ⇒ `schemas` before `securitySchemes` on wire.

**MUST-29 — alias rewrite**: `by_alias=True` ⇒ `schema_`→`schema`, `in_`→`in`, `ref`→`$ref`, `not_`→`not`, `if_`→`if`, `else_`→`else`, `schema_`(in `Schema`)→`$schema`, `type_`→`type`, `id`→`$id`, `defs`→`$defs`, etc. (every aliased field in `models.py`).

**MUST-30 — `exclude_none=True`**: any `None`-valued field is dropped (recursively, also in nested dicts: `encoders.py:295` `value is not None or not exclude_none`). This is why optional scheme fields (`bearerFormat`, `description`, `refreshUrl`) vanish when unset. **NOT `exclude_unset`** — defaults that are non-None ARE emitted (e.g. `OAuthFlow.scopes={}` appears even if never set).

**MUST-31 — `BaseModelWithConfig` has `model_config={"extra":"allow"}`** (`models.py:57-58`) ⇒ unknown keys passed into models (e.g. spec extensions, `openapi_extra`) survive `model_dump`. `Reference`/`Discriminator` are plain `BaseModel` (no extra).

### 3.6 OperationId / unique_id (`utils.py:80-99`) — KNOWN

**MUST-32** `generate_unique_id(route)`: `operation_id = re.sub(r"\W", "_", f"{route.name}{route.path_format}")` then `+ "_" + list(route.methods)[0].lower()`. E.g. route `read_items` path `/items/` GET ⇒ `read_items_items__get` (double `_` from trailing `/`). Path `/path_examples/{item_id}` ⇒ `path_examples_path_examples__item_id__get`. `route.name` defaults to the endpoint function `__name__`.

---

## 4. Parameters, requestBody, examples, responses — exact object shapes

### 4.1 Parameter objects (`_get_openapi_operation_parameters`, `openapi/utils.py:107-177`) — KNOWN

For each of path/query/header/cookie params (in that group order, `utils.py:122-127`):
- `param_schema = get_schema_from_model_field(...)`.
- `name = get_validation_alias(param)`; **MUST-33** header underscore→dash: if `param_type==header and name==param.name and convert_underscores` ⇒ `name = param.name.replace("_","-")` (`utils.py:153-158`). Default `convert_underscores=True` unless a single header param is a `BaseModel` with `convert_underscores=False`.
- `parameter = {"name": name, "in": param_type.value, "required": param.field_info.is_required(), "schema": param_schema}` — **key order fixed: `name, in, required, schema`** then conditionally `description`, then `examples`(if `openapi_examples`) **xor** `example`(if `field_info.example is not _Unset`), then `deprecated:True`(if truthy). (`utils.py:160-176`).
- `param_schema` itself carries inline JSON-Schema `examples` (list) when `Field(examples=[...])` is used (test `test_openapi_examples.py:206-223`: `"schema":{"type":"string","examples":[...],"title":"Item Id"}`), while `openapi_examples` (named) becomes the sibling `parameter["examples"]` object `{"Name":{"summary","description","value"}}`.

**MUST-34 — parameter dedup** (`utils.py:301-313`): `all_parameters = {(in,name):p}`; `required_parameters = {(in,name):p for required}`; `all_parameters.update(required_parameters)`; `operation["parameters"] = list(all_parameters.values())`. Insertion order preserved by dict; required dup overwrites in place (keeps original position).

### 4.2 requestBody (`get_openapi_operation_request_body`, `openapi/utils.py:180-212`) — KNOWN

- `body_schema = get_schema_from_model_field(body_field, ...)`.
- `request_media_type = field_info.media_type` (JSON ⇒ `application/json`; Form ⇒ `application/x-www-form-urlencoded`; File ⇒ `multipart/form-data`).
- `request_body_oai = {}`; if `required`: `request_body_oai["required"] = True` (only emitted when required — key order: `content` then maybe nothing, but `required` is set BEFORE `content` in dict so wire shows `required` then `content`? — actually `RequestBody` model field order is `description, content, required` ⇒ wire: `content, required`).
- `request_media_content = {"schema": body_schema}`; if `field_info.openapi_examples`: add `["examples"]` (jsonable_encoded); elif `field_info.example is not _Unset`: add `["example"]`. `request_body_oai["content"] = {media_type: request_media_content}`.

**MUST-35 — Body wrapper model naming** (`dependencies/utils.py:1019`): when body params are embedded, `model_name = "Body_" + name` where `name = route.unique_id`. Test `test_security_oauth2.py:181`: `Body_login_login_post`, `$ref:"#/components/schemas/Body_login_login_post"`. Single non-embedded body param uses its own model name (no `Body_` wrapper). `BodyModel` built via `create_model(model_name, **{f.name:(annotation,field_info)})` (`_compat/v2.py:387-392`) — field order = body-param declaration order.

### 4.3 Response content & 422 (`openapi/utils.py:340-474`) — KNOWN

- Default success response: `{"<status>": {"description": route.response_description}}`, `response_description` default `"Successful Response"` (`applications.py` route default).
- JSONResponse with `response_model` ⇒ `content["application/json"]["schema"] = <model $ref or inline>`; no response_model ⇒ `"schema": {}` (empty schema = "any"). Test snapshots: `"content":{"application/json":{"schema":{}}}` for untyped returns.
- **MUST-36** 422 only injected if `(all_route_params or route.body_field)` and none of `422/4XX/default` already present. Verbatim const (`openapi/utils.py:42-69`):
  - `ValidationError`: `{"title":"ValidationError","type":"object","properties":{"loc":{"title":"Location","type":"array","items":{"anyOf":[{"type":"string"},{"type":"integer"}]}},"msg":{"title":"Message","type":"string"},"type":{"title":"Error Type","type":"string"},"input":{"title":"Input"},"ctx":{"title":"Context","type":"object"}},"required":["loc","msg","type"]}`.
  - `HTTPValidationError`: `{"title":"HTTPValidationError","type":"object","properties":{"detail":{"title":"Detail","type":"array","items":{"$ref":"#/components/schemas/ValidationError"}}}}`.
- **MUST-37 — additional `responses`** (`utils.py:410-453`): status key `str(code).upper()` except `"DEFAULT"`→`"default"`; description fallback chain `process_response.description → openapi_response.description → status_code_ranges.get(...) → http.client.responses.get(int(code)) → "Additional Response"`. `status_code_ranges = {"1XX":"Information","2XX":"Success","3XX":"Redirection","4XX":"Client Error","5XX":"Server Error","DEFAULT":"Default Response"}`. `model` key popped, replaced by generated schema via `deep_dict_update`.

### 4.4 422 wire-shape at runtime (cross-ref `fastapi/03 §3.4`) — KNOWN

`RequestValidationError` ⇒ `JSONResponse(status_code=422, content={"detail": [{"type","loc","msg","input"[,"ctx"]}]})`. Test `test_security_oauth2.py:67-130`: missing field ⇒ `{"type":"missing","loc":["body","grant_type"],"msg":"Field required","input":None}`; pattern mismatch ⇒ adds `"ctx":{"pattern":"^password$"}`, `"type":"string_pattern_mismatch"`, `"msg":"String should match pattern '^password$'"`. `loc` prefix is `("body",…)` / `("query",…)` / `("path",…)` / `("header",…)` / `("cookie",…)`. **MUST-38**: errors are produced by pydantic-core `ValidationError.errors(include_url=False)` then loc-prefixed via `_regenerate_error_with_loc` (`_compat/v2.py:486-493`: `{**err, "loc": loc_prefix + err.get("loc",())}`). `missing` errors have `input` forced to `None` (`v2.py:379-384`). Lancelot must call pydantic-core with `include_url=False` and apply identical loc-prefixing.

### 4.5 JSON-Schema edge cases — discriminated unions, anyOf dedup, response titles (KNOWN)

These are the cases that silently break a naive Rust re-implementation. All produced by pydantic-core `GenerateJsonSchema`, assembled by FastAPI.

**MUST-44 — Discriminated unions** (`test_union_body_discriminator.py:71-79`): `Annotated[A|B, Field(discriminator="value")]` (or pydantic `Tag`) ⇒ requestBody schema:
```json
{"oneOf":[{"$ref":"#/components/schemas/FirstItem"},{"$ref":"#/components/schemas/OtherItem"}],
 "discriminator":{"propertyName":"value","mapping":{"first":"#/components/schemas/FirstItem","other":"#/components/schemas/OtherItem"}},
 "title":"Item"}`
```
Note: discriminated union ⇒ **`oneOf`** + `discriminator` object (`propertyName` + `mapping` of literal→`$ref`-string). Plain (non-discriminated) `A|B` ⇒ **`anyOf`** of `$ref`s, NO discriminator (MUST-45). `Field(description=...)` on a *query* scalar surfaces as the parameter's `schema.description` (inline). The `Discriminator` model (`openapi/models.py:99-101`) is a plain `BaseModel` (no `extra=allow`): `{propertyName: str, mapping: dict[str,str]|None}`.

**MUST-45 — Union `anyOf` with NO duplicate `$ref` accumulation** (`test_additional_responses_union_duplicate_anyof.py:64-101`): app-level `responses={500:{"model": ModelA|ModelB, "content":{...examples...}}}` reused across multiple routes ⇒ each route's 500 response schema is:
```json
{"anyOf":[{"$ref":"#/components/schemas/ModelA"},{"$ref":"#/components/schemas/ModelB"}],
 "title":"Response 500 Route1 Route1 Get"}
```
The `anyOf` array must NOT accumulate duplicate `$ref`s when the same response model is shared across routes (regression PR #14463 — `deep_dict_update`'s list-concat behavior, `utils.py` `deep_dict_update` does `main[key]+update[key]` for lists, so FastAPI must build each route's response schema fresh, not mutate a shared dict). Lancelot's per-route schema assembly MUST deep-copy, never mutate a shared response template.

**MUST-46 — Response-model auto title pattern**: a response whose schema is NOT a single `$ref` (i.e. `list[X]`, `A|B`, inline) gets an injected `title = "Response " + <operationId-words>`. Derived from `get_schema_from_model_field` MUST-26 applied to the synthetic response field whose alias = `"Response_" + unique_id`-ish; concretely the wire titles seen: `"Response Read Items Items  Get"` (list response, `test_openapi_separate_input_output_schemas.py:182`), `"Response 500 Route1 Route1 Get"` (union additional-response). The words come from the field alias `.title().replace("_"," ")` — note the **double space** when the path had a trailing `/` (operationId had `__`). Single-model response (`response_model=Model3`) ⇒ pure `{"$ref":"#/components/schemas/Model3"}`, no title (MUST-26: `$ref` schemas get no injected title).

**MUST-47 — `Schema.type` validation** (`openapi/models.py:118-156`, `test_openapi_schema_type.py`): `SchemaType = Literal["array","boolean","integer","null","number","object","string"]`; `Schema.type: SchemaType | list[SchemaType] | None`. `type=True` raises `ValidationError("2 validation errors for Schema")`. JSON-Schema 2020-12 numeric constraints carry pydantic `Field` bounds: `multipleOf` `gt=0`; `maxLength/minLength/maxItems/minItems/maxContains/minContains/maxProperties/minProperties` `ge=0`; `ServerVariable.enum` `min_length=1`. Lancelot's schema model (if it validates spec docs) must reproduce these bounds; if it only *emits* (never validates) it can skip — but `openapi_extra`/spec-extension round-trips rely on `extra=allow` (MUST-31).

**MUST-48 — `anyOf` null ordering**: optional fields (`X | None`) emit `{"anyOf":[{"type":"string"},{"type":"null"}],"title":"…"}` — the `{"type":"null"}` member is **last**, and the wrapper carries `title` but the members do not (`test_openapi_separate_input_output_schemas.py:330-337,348-356,365-368`). Optional `BaseModel` field: `{"anyOf":[{"$ref":"#/components/schemas/SubItem-Input"},{"type":"null"}]}` — note: NO `title` when one member is a `$ref` and pydantic suppresses it. This null-last ordering is pydantic-core's; Lancelot must not sort `anyOf`.

**MUST-49 — default emission in property schemas**: a field with a non-None default emits `"default": <value>` inside its property schema (`test_security_oauth2.py:217-221`: `"scope":{"title":"Scope","type":"string","default":""}`; `test_openapi_separate_input_output_schemas.py:369-374`: `"tags":{...,"default":[]}`). `exclude_none` does NOT strip these (default is non-None). Required fields have NO `default` key and appear in the schema's `"required":[...]` list (declaration order).

---

### 4.6 Body-embed decision — when `Body_<id>` wrapper appears (`dependencies/utils.py:_should_embed_body_fields`) — KNOWN

This decides whether the requestBody is a single `$ref` (one model param) or a synthetic `Body_<unique_id>` wrapper object — directly affects `components.schemas` keys and `requestBody.content.schema`.

**MUST-50** `_should_embed_body_fields(fields)`:
1. `if not fields: return False` (no body).
2. `body_param_names_set = {f.name for f in fields}`; **`if len(set) > 1: return True`** — ≥2 distinct body params ⇒ embed ⇒ wrapper `Body_<unique_id>` (test `test_security_oauth2.py:205` `Body_login_login_post`, fields `grant_type,username,password,scope,client_id,client_secret`).
3. `if getattr(first_field.field_info, "embed", None): return True` — explicit `Body(embed=True)`.
4. `if isinstance(first_field.field_info, params.Form) and not lenient_issubclass(annotation, BaseModel) and not is_union_of_base_models(annotation): return True` — a scalar/non-model Form field must be embedded so the form key can be extracted (this is why `OAuth2PasswordRequestForm` ⇒ `Body_login_login_post` even with its fields).
5. else `return False` — single model body param ⇒ direct `$ref` to that model (test `test_openapi_separate_input_output_schemas.py:196` `{"$ref":"#/components/schemas/Item-Input"}`, no `Body_` wrapper).

**MUST-51** When embedded, `get_body_field` (`dependencies/utils.py:1019-1052`) builds `model_name = "Body_" + unique_id`, `create_model(model_name, **{f.name:(annotation,field_info)})`, `alias="body"`, `required = any(f.is_required())`, media type = `File` if any File else `Form` if any Form else `Body` (single common media type if all match). The wrapper model's JSON-Schema is generated by pydantic like any model ⇒ appears in sorted `components.schemas`.

### 4.7 Security scheme description / optional variants — KNOWN

**MUST-52 — scheme object key order with description**: the security scheme model object is `jsonable_encoder(scheme.model, by_alias=True, exclude_none=True)`:
- `APIKey`: **CONFIRMED wire order = `type, name, in [, description]`** (snapshot oracle: `test_security_api_key_header.py` `{"type":"apiKey","name":"key","in":"header"}` and `test_security_api_key_header_description.py` adds `"description":"An API Key Header"` last). The wire order is empirically established; do NOT derive from source field-line order (pydantic v2's effective `model_fields` ordering for an inheriting model that redeclares an inherited field is subtle — treat the snapshots as ground truth). `exclude_none` drops `description=None`. **Lancelot conformance harness must pin scheme key order to the snapshot, not to a re-derivation.**
- `OAuth2`: `{"type":"oauth2","flows":{...}[,"description":"…"]}` (`test_security_oauth2_optional_description.py:267-278`).
- `OpenIdConnect`: `{"type":"openIdConnect","openIdConnectUrl":"…"[,"description":…]}`.
- HTTP: `{"type":"http","scheme":"…"[,"bearerFormat":"…"][,"description":…]}`.

**MUST-53 — `description` on a scheme** is set on the *model* (`APIKey(description=…)` etc.) ⇒ appears in `securitySchemes` only (NOT the 401 response). `auto_error=False` changes runtime behavior only (returns `None`); the OpenAPI scheme object + `security:[{Name:[]}]` requirement are unchanged (test `test_security_oauth2_optional_description.py`). Optional auth still lists the security requirement; the dependency returning `None` is the user's concern.

### 4.8 CORS — Starlette passthrough (table-stakes note) — KNOWN

`fastapi/middleware/cors.py:1` is a one-line re-export: `from starlette.middleware.cors import CORSMiddleware as CORSMiddleware`. **FastAPI adds zero CORS logic.** Lancelot's CORS (table-stakes, `DESIGN-DECISIONS §3`) must replicate **Starlette's** `CORSMiddleware` semantics, not a FastAPI layer: preflight `OPTIONS` short-circuit with `Access-Control-Allow-{Origin,Methods,Headers,Credentials,Max-Age}`, origin allowlist/regex/`*`, `Vary: Origin`, simple-request header injection, `allow_credentials` ⇒ echo origin (never `*`). CORS never appears in OpenAPI. (Out of scope for byte-identical OpenAPI; in scope for byte-identical wire behavior — implement in the Rust middleware tier per `DESIGN-DECISIONS §1 fork #3`.)

## 5. Docs HTML / CDN params (`openapi/docs.py`, `applications.py:1101-1154`) — KNOWN

### 5.1 Endpoint defaults (`applications.py`)
- `openapi_url = "/openapi.json"` (`:220`), `docs_url = "/docs"` (`:420`), `redoc_url = "/redoc"` (`:444`), `swagger_ui_oauth2_redirect_url = "/docs/oauth2-redirect"` (`:457`).
- `title = "FastAPI"` (`:114`), `version = "0.1.0"` (`:197`), `openapi_version = "3.1.0"` (`:921`).
- **MUST-39** `/openapi.json` handler (`applications.py:1104-1116`): `JSONResponse(schema)`; if `root_path` and `root_path_in_servers`: prepend `{"url": root_path}` to `servers` (only if not already present). `root_path` is `req.scope["root_path"].rstrip("/")`.
- **MUST-40** `/docs` builds `openapi_url = root_path + self.openapi_url`; `oauth2_redirect_url = root_path + self.swagger_ui_oauth2_redirect_url` (if set); calls `get_swagger_ui_html(openapi_url, title=f"{self.title} - Swagger UI", oauth2_redirect_url, init_oauth=self.swagger_ui_init_oauth, swagger_ui_parameters=self.swagger_ui_parameters)`.
- `/redoc` ⇒ `get_redoc_html(openapi_url=root_path+openapi_url, title=f"{self.title} - ReDoc")`.
- `/docs/oauth2-redirect` ⇒ `get_swagger_ui_oauth2_redirect_html()` (static HTML, only added if `swagger_ui_oauth2_redirect_url` set).
- All docs routes `include_in_schema=False`. Content-Type `text/html; charset=utf-8` (Starlette `HTMLResponse`).

### 5.2 CDN constants (`openapi/docs.py`) — KNOWN, FRESHNESS-SENSITIVE
- `swagger_js_url = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"` (`:79`).
- `swagger_css_url = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"` (`:92`).
- `swagger_favicon_url = redoc_favicon_url = "https://fastapi.tiangolo.com/img/favicon.png"` (`:100,244`).
- `redoc_js_url = "https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"` (`:236`).
- ReDoc Google Fonts (default `with_google_fonts=True`): `https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700` (`:275`).

### 5.3 Swagger UI default params + HTML structure (`docs.py:22-194`) — KNOWN
- `swagger_ui_default_parameters = {"dom_id":"#swagger-ui","layout":"BaseLayout","deepLinking":True,"showExtensions":True,"showCommonExtensions":True}` (`:31-37`). User `swagger_ui_parameters` `.update()` over this copy.
- **MUST-41 — XSS escaping** `_html_safe_json(value)` (`docs.py:9-19`): `json.dumps(value).replace("<","\\u003c").replace(">","\\u003e").replace("&","\\u0026")`. Applied to every param key/value and to `init_oauth`. Test `test_swagger_ui_escape.py`: `</script>` ⇒ `</script>`.
- HTML body (`docs.py:152-194`): `<!DOCTYPE html><html><head>` `<meta viewport>` `<link stylesheet href="{css}">` `<link shortcut icon href="{favicon}">` `<title>{title}</title>` `</head><body><div id="swagger-ui"></div><script src="{js}"></script><script>const ui = SwaggerUIBundle({ url: '{openapi_url}',` then per-param `"{k}": {v},\n` then (if oauth2_redirect_url) `oauth2RedirectUrl: window.location.origin + '{oauth2_redirect_url}',` then `presets:[SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],})` then (if init_oauth) `ui.initOAuth({...})` then `</script></body></html>`.
- Test `test_application.py:24-32`: `/docs` contains `swagger-ui-dist` and literal `oauth2RedirectUrl: window.location.origin + '/docs/oauth2-redirect'`. `test_swagger_ui_init_oauth.py`: `ui.initOAuth` + `"appName": "The Predendapp"` present.
- **MUST-42** ReDoc HTML (`docs.py:264-298`): `<redoc spec-url="{openapi_url}"></redoc><script src="{redoc_js_url}"></script>`, `<noscript>` fallback, inline `body{margin:0;padding:0}` style, Google Fonts `<link>` only if `with_google_fonts`.
- **MUST-43** OAuth2 redirect HTML (`docs.py:301-389`) is a **verbatim static blob** copied from swagger-ui v4.14.0 `oauth2-redirect.html` — Lancelot must ship this exact string (it parses `window.location.hash`, validates `state`, handles `authorizationCode`/`accessCode` flows, calls `oauth2.callback`/`errCb`, `window.close()`). Any change breaks Swagger's OAuth2 flow.

---

## 6. Conformance checklist — numbered MUST rules for byte-identical OpenAPI + auth

> Pass criteria: serialize both documents with the same JSON encoder (sorted? NO — see MUST-28; preserve model field order) and `assert text == text`. FastAPI's own tests use dict `==` which is order-insensitive; Lancelot's harness MUST string-compare.

**Security extraction & errors (1–12, repeated from §1.6 — the hard auth gate):**
1. Authorization parse = `partition(" ")` first-space, `.strip()` param, empty header → `("","")` (MUST-1).
2. APIKey 401 + `WWW-Authenticate: APIKey` [FORK] (MUST-2/3/4).
3. HTTPBase 401 + `WWW-Authenticate: <scheme>.title()` (MUST; HTTPDigest ⇒ `Digest`).
4. HTTPBasic 401 + `Basic` / `Basic realm="R"`; bad-b64/no-`:` ⇒ 401 even if auto_error=False (MUST-5/6/12).
5. HTTPBearer two-step (presence then `scheme==bearer`), both ⇒ 401+`Bearer` [FORK: was 403] (MUST-7).
6. OAuth2 base & OpenIdConnect return **raw full Authorization header**; PasswordBearer/AuthCodeBearer return **token only** (MUST-9).
7. All auth bodies = `{"detail":"Not authenticated"}`, status **401** always (never 403) [FORK] (MUST-11).
8. `auto_error=False` ⇒ `None` (HTTPBearer/Digest/OAuth2Bearer: also on scheme mismatch) (MUST-7/12).
9. FastAPI does **NOT** enforce scopes — only collects into `SecurityScopes` (MUST-13/15).
10. Scope OpenAPI order = first-seen+dedup (NOT sorted); cache-key scopes = sorted+set (MUST-14).
11. `SecurityScopes(scopes=dependant.oauth_scopes)`, `.scope_str=" ".join(scopes)` (MUST-15).
12. Security-dep cache de-dup keyed on `(call, sorted(set(scopes)) if _uses_scopes else (), computed_scope)` (MUST-16).

**OpenAPI document structure (13–38):**
13. `openapi` default `"3.1.0"`; `info={"title","version"}` order, `version` last in `Info` model (MUST-28).
14. `$ref` = `#/components/schemas/{model}`; `REF_PREFIX`/`REF_TEMPLATE` exact (MUST-21).
15. `components.schemas` keys **sorted lexicographically**; `securitySchemes` in route order (MUST-17).
16. Wire key order follows **Pydantic model field declaration order** (`OpenAPI`/`Operation`/`Info`/`Parameter`/`Components`/`RequestBody` in `models.py`), NOT build-dict order — `paths` before `components`, `schemas` before `securitySchemes` (MUST-28).
17. `by_alias=True` rewrites (`schema_→schema`, `in_→in`, `ref→$ref`, `not_→not`, `$defs`, `$schema`, `type→type`…) (MUST-29).
18. `exclude_none=True` drops all `None` recursively; non-None defaults (e.g. `scopes={}`) ARE emitted (MUST-30).
19. `BaseModelWithConfig` extra=allow keeps spec extensions / `openapi_extra` (MUST-31).
20. operationId = `re.sub(r"\W","_",f"{name}{path_format}")+"_"+method.lower()` (MUST-32).
21. summary fallback = `route.name.replace("_"," ").title()` (MUST-18).
22. response description default `"Successful Response"`; status from `route.status_code` or response-class `__init__` default (MUST-19).
23. Input/Output schema split = pydantic `GenerateJsonSchema` behavior; only when `separate_input_output_schemas=True` and modes differ; collapse to single name otherwise (MUST-22/24).
24. description truncated at first `\f` (MUST-23).
25. `bytes` ⇒ `{"type":"string","contentMediaType":"application/octet-stream"[,"contentEncoding":"base64"]}` (MUST-25).
26. inline (non-`$ref`) field schema gets injected `title = field_info.title or alias.title().replace("_"," ")`; `$ref` gets none (MUST-26).
27. model name normalize `re.sub(r"[^a-zA-Z0-9.\-_]","_",__name__)` (MUST-27).
28. Parameter object key order `name, in, required, schema [, description] [, examples|example] [, deprecated]` (MUST-33).
29. header param name `_`→`-` when name==field name and convert_underscores (MUST-33).
30. parameter dedup `{(in,name):p}` with required-overrides-nonrequired (MUST-34).
31. `Body_<unique_id>` wrapper model for embedded body; field order = declaration order (MUST-35).
32. requestBody media type by Form/File/JSON; `required:True` only when required (MUST-35).
33. 422 injected iff params/body present and no 422/4XX/default; exact `ValidationError`/`HTTPValidationError` consts (MUST-36).
34. additional-responses status uppercased (`DEFAULT→default`), description fallback chain, `status_code_ranges` map, `model` popped (MUST-37).
35. 422 runtime body `{"detail":[{type,loc,msg,input[,ctx]}]}`, loc-prefixed, `include_url=False`, missing.input=None (MUST-38).
36. METHODS_WITH_BODY = `{GET,HEAD,POST,PUT,DELETE,PATCH}` (no OPTIONS/TRACE) (MUST-19).
37. `is_body_allowed_for_status_code`: False for `<200`,`204`,`205`,`304` (MUST: response content suppressed).
38. `[FORK]` streaming: `application/jsonl` (`{"itemSchema":…}`) for JSONL; `text/event-stream` with `_SSE_EVENT_SCHEMA` deep-copy, `data` rewritten to `{type:string,contentMediaType:application/json,contentSchema:…}` + `required:["data"]` when stream item typed (MUST: §3.2).

**Docs / CDN (39–43):**
39. Endpoint defaults `/openapi.json` `/docs` `/redoc` `/docs/oauth2-redirect`; titles `"{title} - Swagger UI"` / `"{title} - ReDoc"`; root_path prefixing + `servers` injection (MUST-39/40).
40. CDN: `swagger-ui-dist@5` js/css, `redoc@2`, favicon `fastapi.tiangolo.com/img/favicon.png`, Montserrat/Roboto fonts (MUST: §5.2).
41. `swagger_ui_default_parameters` exact dict; user params `.update()` over copy (MUST: §5.3).
42. `_html_safe_json`: `<`→`<`, `>`→`>`, `&`→`&` on every param + init_oauth (MUST-41).
43. OAuth2-redirect HTML = verbatim swagger-ui v4.14.0 blob (MUST-43); ReDoc `<redoc spec-url>` + `<noscript>` + `body{margin:0;padding:0}` (MUST-42).

**JSON-Schema edge cases (44–53):**
44. Discriminated union ⇒ `oneOf` + `discriminator:{propertyName, mapping:{literal:$ref-string}}`; `Discriminator` is plain BaseModel (no extra) (MUST-44).
45. Plain union ⇒ `anyOf` of `$ref`s, no discriminator; shared response models must be deep-copied per route (no duplicate `$ref` accumulation) (MUST-45).
46. Non-`$ref` response schema gets injected `title="Response <words>"` (alias `.title().replace("_"," ")`, double-space on trailing-slash paths); single-model response = bare `$ref`, no title (MUST-46).
47. `Schema.type` ∈ `Literal[7 types]|list|None`; pydantic numeric bounds (`multipleOf gt=0`, `*Length/*Items/* ge=0`, `ServerVariable.enum min_length=1`) (MUST-47).
48. `X|None` ⇒ `{"anyOf":[{...},{"type":"null"}],"title":…}` — null member **last**, members untitled; `$ref|None` ⇒ no wrapper title; never sort `anyOf` (MUST-48).
49. Non-None default ⇒ `"default":<v>` in property schema (not stripped by exclude_none); required ⇒ no default + listed in `"required"` (decl order) (MUST-49).
50. Body-embed: ≥2 distinct body params OR `embed=True` OR scalar Form ⇒ `Body_<unique_id>` wrapper; single model param ⇒ direct `$ref` (MUST-50).
51. Wrapper model `create_model("Body_"+unique_id, **{name:(ann,info)})`, alias `body`, media File>Form>Body (MUST-51).
52. Scheme object key order (snapshot-pinned): apiKey `type,name,in[,description]`; oauth2 `type,flows[,description]`; openIdConnect `type,openIdConnectUrl[,description]`; http `type,scheme[,bearerFormat][,description]` (MUST-52).
53. `auto_error=False` changes runtime (returns None) but NOT the OpenAPI scheme object or `security` requirement; description set on model only (MUST-53).

**CORS (54):**
54. FastAPI CORS = Starlette `CORSMiddleware` re-export verbatim; Lancelot replicates Starlette semantics in the Rust middleware tier; CORS never in OpenAPI (§4.8).

---

## 6A. Verbatim reimplementation artifacts (load-bearing — copy exactly)

These must be byte-identical in Lancelot; reproduced here so the implementer needs no second lookup.

### 6A.1 Security `__call__` decision trees (Rust state-machine spec, KNOWN — from `security/*`)

```text
# All return either <value> (continue) or RAISE 401 / return None (auto_error gate)
APIKey{Query|Header|Cookie}(request):
    v = request.{query_params|headers|cookies}.get(model.name)   # headers: case-insensitive
    if not v:                       # "" counts as missing
        if auto_error: RAISE HTTPException(401, "Not authenticated", {"WWW-Authenticate":"APIKey"})
        else: return None
    return v

HTTPBase(request):
    auth = headers.get("Authorization")
    scheme, creds = partition_first_space(auth)        # ("","") if no auth
    if not (auth and scheme and creds):
        if auto_error: RAISE 401 {"WWW-Authenticate": scheme_attr.title()}   # scheme is the ctor scheme
        else: return None
    return HTTPAuthorizationCredentials(scheme=scheme, credentials=creds)

HTTPBasic(request):
    auth = headers.get("Authorization"); scheme, param = partition_first_space(auth)
    if not auth or scheme.lower() != "basic":
        if auto_error: RAISE 401 {"WWW-Authenticate": realm? f'Basic realm="{realm}"' : "Basic"}
        else: return None
    try: data = b64decode(param).decode("ascii")
    except (ValueError|UnicodeDecodeError|binascii.Error): RAISE 401 {WWW-Auth as above}   # NO auto_error gate
    user, sep, pwd = data.partition(":")
    if not sep: RAISE 401 {WWW-Auth as above}                                              # NO auto_error gate
    return HTTPBasicCredentials(username=user, password=pwd)

HTTPBearer(request):
    auth = headers.get("Authorization"); scheme, creds = partition_first_space(auth)
    if not (auth and scheme and creds):
        if auto_error: RAISE 401 {"WWW-Authenticate":"Bearer"}   # [FORK] (was 403 stock)
        else: return None
    if scheme.lower() != "bearer":
        if auto_error: RAISE 401 {"WWW-Authenticate":"Bearer"}   # [FORK]
        else: return None
    return HTTPAuthorizationCredentials(scheme=scheme, credentials=creds)

HTTPDigest(request): identical to HTTPBearer but scheme.lower() != "digest", WWW-Authenticate "Digest"

OAuth2(base)(request):
    auth = headers.get("Authorization")
    if not auth:
        if auto_error: RAISE 401 {"WWW-Authenticate":"Bearer"}
        else: return None
    return auth                       # RAW FULL HEADER STRING

OAuth2PasswordBearer / OAuth2AuthorizationCodeBearer(request):
    auth = headers.get("Authorization"); scheme, param = partition_first_space(auth)
    if not auth or scheme.lower() != "bearer":
        if auto_error: RAISE 401 {"WWW-Authenticate":"Bearer"}
        else: return None
    return param                      # TOKEN ONLY (no scheme)

OpenIdConnect(request):
    auth = headers.get("Authorization")
    if not auth:
        if auto_error: RAISE 401 {"WWW-Authenticate":"Bearer"}
        else: return None
    return auth                       # RAW FULL HEADER STRING
```
The raised body is always Starlette `HTTPException(401, detail="Not authenticated")` ⇒ `{"detail":"Not authenticated"}`.

### 6A.2 422 schema constants — emit verbatim (KNOWN, `openapi/utils.py:42-69`)

```json
"ValidationError": {"title":"ValidationError","type":"object",
  "properties":{"loc":{"title":"Location","type":"array","items":{"anyOf":[{"type":"string"},{"type":"integer"}]}},
    "msg":{"title":"Message","type":"string"},"type":{"title":"Error Type","type":"string"},
    "input":{"title":"Input"},"ctx":{"title":"Context","type":"object"}},
  "required":["loc","msg","type"]}
"HTTPValidationError": {"title":"HTTPValidationError","type":"object",
  "properties":{"detail":{"title":"Detail","type":"array","items":{"$ref":"#/components/schemas/ValidationError"}}}}
```
Note: in the *runtime* 422 body these become `{"detail":[{"type","loc","msg","input"[,"ctx"]}]}` (different — the const above is the OpenAPI *documentation* of the error, not the wire body).

### 6A.3 `_SSE_EVENT_SCHEMA` (KNOWN, `sse.py:9`) + streaming content blocks

```json
_SSE_EVENT_SCHEMA = {"type":"object","properties":{"data":{"type":"string"},"event":{"type":"string"},
                     "id":{"type":"string"},"retry":{"type":"integer","minimum":0}}}
```
- JSONL route ⇒ `responses[sc].content["application/jsonl"] = {"itemSchema": <stream_item schema> or {}}`.
- SSE route untyped ⇒ `content["text/event-stream"] = {"itemSchema": <deepcopy(_SSE_EVENT_SCHEMA)>}`.
- SSE route typed (`stream_item_field`) ⇒ deepcopy then `item["required"]=["data"]`, `item["properties"]["data"]={"type":"string","contentMediaType":"application/json","contentSchema":<field schema>}`; wrap as `{"itemSchema": item}`.

### 6A.4 Swagger UI HTML template (KNOWN, `openapi/docs.py:152-194`) — exact f-string skeleton

```html
<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link type="text/css" rel="stylesheet" href="{swagger_css_url}">
<link rel="shortcut icon" href="{swagger_favicon_url}">
<title>{title}</title></head><body><div id="swagger-ui"></div>
<script src="{swagger_js_url}"></script>
<script>const ui = SwaggerUIBundle({ url: '{openapi_url}',
{ for k,v in (defaults | user_params): }"{esc(k)}": {esc(jsonable_encoder(v))},\n
{ if oauth2_redirect_url: }oauth2RedirectUrl: window.location.origin + '{oauth2_redirect_url}',
presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],})
{ if init_oauth: }ui.initOAuth({esc(jsonable_encoder(init_oauth))})
</script></body></html>
```
`esc()` = `_html_safe_json` (MUST-41). Defaults dict (MUST-41): `{"dom_id":"#swagger-ui","layout":"BaseLayout","deepLinking":true,"showExtensions":true,"showCommonExtensions":true}`. `title` arg passed by app = `"{app.title} - Swagger UI"`. The leading whitespace/newlines in the source f-string are part of the output (the test only does substring asserts, so exact whitespace is not snapshot-pinned — but Lancelot should match the structure; `test_application.py` asserts the literal `oauth2RedirectUrl: window.location.origin + '/docs/oauth2-redirect'`).

### 6A.5 ReDoc HTML template (KNOWN, `openapi/docs.py:264-298`)

```html
<!DOCTYPE html><html><head><title>{title}</title>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1">
{ if with_google_fonts: }<link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">
<link rel="shortcut icon" href="{redoc_favicon_url}">
<style>body {margin:0;padding:0;}</style></head>
<body><noscript>ReDoc requires Javascript to function. Please enable it to browse the documentation.</noscript>
<redoc spec-url="{openapi_url}"></redoc><script src="{redoc_js_url}"> </script></body></html>
```
`title` arg = `"{app.title} - ReDoc"`. Note the literal space inside `</script>` → `"> </script>"` (preserve it).

### 6A.6 OAuth2-redirect HTML (KNOWN, `openapi/docs.py:308-388`) — ship VERBATIM

Static, parameter-free. Copied from swagger-ui v4.14.0 `dist/oauth2-redirect.html`. Key logic: reads `window.location.hash` (or `location.search`) for `code|token|error`, parses to `qp`, `isValid = qp.state === sentState`, branches on `oauth2.auth.schema.get("flow") ∈ {accessCode, authorizationCode, authorization_code}`: if code-flow & no code → if `qp.code` set `oauth2.auth.code` + `oauth2.callback(...)` else build `oauthErrorMsg` from `qp.error/error_description/error_uri` and `oauth2.errCb(...)`; else `oauth2.callback({auth, token:qp, isValid, redirectUrl})`; finally `window.close()`. Runs on `DOMContentLoaded` or immediately if not loading. **Lancelot must embed this exact ~80-line blob** (full text at `openapi/docs.py:308-388`); any edit breaks Swagger's Authorization-Code/Implicit flows.

## 7. Lancelot replication plan — KNOWN / DESIGN split

**DESIGN — the seam decision (locks `DESIGN-DECISIONS §1 fork #4`):**
- **Schema generation MUST be delegated to pydantic-core's `GenerateJsonSchema` via FFI**, with FastAPI's `bytes_schema` subclass override re-applied. Re-implementing JSON-Schema-2020-12 + `$defs` collision + `-Input`/`-Output` suffixing + computed-field `readOnly` in Rust would drift on every pydantic patch release. Lancelot calls the *same Python `GenerateJsonSchema(ref_template="#/components/schemas/{model}")`* once at registration; the result is frozen into the compiled route plan. (Matches `SYNTHESIS §2 #6-7`: validation delegated to pydantic-core.)
- **The OpenAPI document assembly (`get_openapi`/`get_openapi_path` logic) CAN be re-implemented in Rust** — it is deterministic dict manipulation with no pydantic dependency *except* the final `jsonable_encoder(OpenAPI(**output))` pass. DESIGN: build the dict in Rust in the **Pydantic-model field order** directly (skip the round-trip through the `OpenAPI` model), apply alias rewrite + `exclude_none` as a Rust pass. This removes the per-`/openapi.json` `jsonable_encoder` Python walk (cross-ref `fastapi/03 §0.3` — `jsonable_encoder` on the hot path is an AVOID). The schema is computed once at boot and cached as bytes (const-tier eligible — `DESIGN-DECISIONS §1 fork #5`).
- **Security schemes** are pure Rust: header lookup + `partition`/`b64decode` + the 401-or-None decision tree is trivial and GIL-free. The returned credential object (`HTTPBasicCredentials`/`HTTPAuthorizationCredentials`/str) crosses into Python only as the dependency's resolved value. DESIGN: implement the §1.6 matrix as a Rust state machine; `make_*_error` produces a pre-baked 401 response (zero Python).
- **Scope plumbing** is registration-time DAG metadata (own/parent scopes) — fits the two-phase DI compiler (`DESIGN-DECISIONS §4.4`). `SecurityScopes` is the only request-time object; build it in Rust, hand to Python.

**KNOWN gaps Lancelot must calibrate against the real API:**
- pydantic version skew: `-Input`/`-Output` suffix rules, `$defs` numbering, `anyOf` null ordering, default-injection all depend on the exact pinned pydantic-core. Calibrate by diffing `/openapi.json` byte-for-byte against a reference FastAPI of the pinned version across the test corpus (`tests/test_openapi_*`, `tests/test_security_*` snapshots are the oracle).
- This is a **fork** — the `WWW-Authenticate`-always + 401-always + `application/jsonl`/`text/event-stream` `itemSchema` behavior differs from stock FastAPI. Lancelot must target *this fork's* snapshots, not pip `fastapi`.

## 7A. Pydantic model field-order reference (the wire-order source of truth, KNOWN — `openapi/models.py`)

`model_dump(by_alias=True, exclude_none=True)` emits keys in these orders. Lancelot must build the dict in *this* order directly (skip the model round-trip per §7 DESIGN). `(alias)` = wire key after `by_alias`.

| Model | Field order (wire keys) | Source |
|---|---|---|
| `OpenAPI` | `openapi, info, jsonSchemaDialect, servers, paths, webhooks, components, security, tags, externalDocs` | `models.py:419-431` |
| `Info` | `title, summary, description, termsOfService, contact, license, version` | `models.py:73-80` |
| `Contact` | `name, url, email` (`+extra`) | `models.py:61-65` |
| `License` | `name, identifier, url` | `models.py:67-70` |
| `Server` | `url, description, variables` | `models.py:89-92` |
| `PathItem` | `$ref(ref), summary, description, get, put, post, delete, options, head, patch, trace, servers, parameters` | `models.py:305-318` |
| `Operation` | `tags, summary, description, externalDocs, operationId, parameters, requestBody, responses, callbacks, deprecated, security, servers` | `models.py:289-302` |
| `Parameter` | `description, required, deprecated, style, explode, allowReserved, schema(schema_), example, examples, content, name, in(in_)` | `models.py:243-261` |
| `RequestBody` | `description, content, required` | `models.py:267-270` |
| `Response` | `description, headers, content, links` | `models.py:282-286` |
| `MediaType` | `schema(schema_), example, examples, encoding` | `models.py:236-240` |
| `Components` | `schemas, responses, parameters, examples, requestBodies, headers, securitySchemes, links, callbacks, pathItems` | `models.py:399-410` |
| `APIKey` | `type(type_), name, in(in_), description` — **snapshot-pinned** (MUST-52) | `models.py:328-342` |
| `HTTPBase`/`HTTPBearer` | `type, scheme[, bearerFormat], description` | `models.py:345-352` |
| `OAuth2` | `type, flows, description` | `models.py:384-386` |
| `OpenIdConnect` | `type, openIdConnectUrl, description` | `models.py:389-393` |
| `OAuthFlow*` | `refreshUrl, scopes[, authorizationUrl][, tokenUrl]` (subclass adds URL fields after) | `models.py:355-374` |
| `Tag` | `name, description, externalDocs` | `models.py:413-416` |
| `Discriminator` | `propertyName, mapping` (plain BaseModel, no extra) | `models.py:99-101` |
| `Reference` | `$ref(ref)` only (plain BaseModel) | `models.py:95-96` |

Aliases (`by_alias`): `schema_→schema`, `in_→in`, `ref→$ref`, `type_→type`, `not_→not`, `if_→if`, `else_→else`, plus `Schema`: `$schema,$vocabulary,$id,$anchor,$dynamicAnchor,$ref,$dynamicRef,$defs,$comment`. `exclude_none` drops every `None` recursively (also inside plain dicts, `encoders.py:295`).

## 7B. Worked end-to-end trace — one route → final JSON (KNOWN, reproduced from `test_security_http_bearer.py`)

Route: `@app.get("/users/me") def read_current_user(credentials: HTTPAuthorizationCredentials = Security(HTTPBearer()))`. Trace `get_openapi`:

1. `info = {"title":"FastAPI","version":"0.1.0"}` (defaults).
2. `output = {"openapi":"3.1.0","info":info}`.
3. `all_fields = get_fields_from_routes([route])` → no body/response models, no params (HTTPBearer is a security dep, not a param) ⇒ `[]`.
4. `flat_models = {}`; `model_name_map = {}`; `get_definitions([], {}, True)` ⇒ `({}, {})`.
5. `get_openapi_path(route)`:
   - `operation = {"summary":"Read Current User","operationId":"read_current_user_users_me_get"}` (name `read_current_user`.title-words; unique_id from MUST-32).
   - `flat_dependant = get_flat_dependant(route.dependant, skip_repeats=True)`; `_security_dependencies = [HTTPBearer dep]`.
   - `get_openapi_security_definitions`: `jsonable_encoder(HTTPBearer().model, by_alias=True, exclude_none=True)` = `{"type":"http","scheme":"bearer"}` (bearerFormat None dropped, description None dropped); `scheme_name="HTTPBearer"`; no oauth_scopes ⇒ `operation_security=[{"HTTPBearer":[]}]`. `security_schemes={"HTTPBearer":{"type":"http","scheme":"bearer"}}`.
   - `operation.setdefault("security",[]).extend([{"HTTPBearer":[]}])`.
   - no parameters; method `GET` ∈ METHODS_WITH_BODY but `route.body_field` None ⇒ no requestBody.
   - status `"200"`; `responses["200"]["description"]="Successful Response"`.
   - `is_body_allowed_for_status_code(None)` → True; `JSONResponse` subclass, no `response_field` ⇒ `responses["200"]["content"]["application/json"]["schema"]={}`.
   - 422 gate: `all_route_params` empty AND `route.body_field` None ⇒ **NO 422 injected** (this is why bearer-only routes have no 422).
   - `path["get"] = operation`.
6. `paths["/users/me"] = {"get": operation}`; `components["securitySchemes"]={"HTTPBearer":{...}}`. No `definitions` ⇒ no `components["schemas"]`.
7. `output = {"openapi":"3.1.0","info":…,"components":{"securitySchemes":…},"paths":…}`.
8. `jsonable_encoder(OpenAPI(**output), by_alias=True, exclude_none=True)` reorders to `OpenAPI` field order: `openapi, info, paths, components` (no servers/webhooks/tags/externalDocs).

Final (matches `test_security_http_bearer.py:42-65` exactly, modulo dict-order which the wire fixes to `openapi,info,paths,components`; within `Operation`: `summary,operationId,responses,security`):
```json
{"openapi":"3.1.0","info":{"title":"FastAPI","version":"0.1.0"},
 "paths":{"/users/me":{"get":{"summary":"Read Current User","operationId":"read_current_user_users_me_get",
   "responses":{"200":{"description":"Successful Response","content":{"application/json":{"schema":{}}}}},
   "security":[{"HTTPBearer":[]}]}}},
 "components":{"securitySchemes":{"HTTPBearer":{"type":"http","scheme":"bearer"}}}}
```
**Reimplementation check**: a Lancelot OpenAPI builder that produces this exact byte string (with `Operation` keys in model order `summary,operationId,responses,security` and top-level `openapi,info,paths,components`) for this route passes. Repeat against every `tests/test_security_*` + `tests/test_openapi_*` snapshot (string-compared) to certify byte-fidelity.

## 8. Verdict

**Byte-identical OpenAPI: FEASIBLE.** The document is fully deterministic given (a) the route table, (b) the pinned pydantic-core, (c) faithful reproduction of the assembly order + alias/exclude_none pass + Pydantic-model-field key ordering. **The single hardest part is the `components.schemas` bodies** — they are pydantic-core `GenerateJsonSchema` output, not FastAPI logic; Lancelot MUST call the same generator (FFI) rather than re-implement it, or it will diverge on discriminated unions, computed fields, `$defs` collisions, and default-required serialization. The auth layer is easy and pure-Rust. Everything else (operationId, 422 const, `Body_` naming, docs HTML, XSS escape, CDN strings) is mechanically reproducible from the cited line numbers. Secondary risk: the FastAPI tests compare dicts (order-insensitive); Lancelot's conformance harness must string-compare serialized JSON, because the wire order is the Pydantic model field order (MUST-28) and that is exactly where a naive re-implementation silently drifts.

---

## Appendix — file:line evidence index (KNOWN)

| Topic | File:line |
|---|---|
| Authorization parser | `security/utils.py:1-7` |
| APIKey extraction + `WWW-Authenticate: APIKey` [FORK] | `security/api_key.py:31-52,142-144,230-232,318-320` |
| SecurityBase | `security/base.py:1-6` |
| HTTPBase/Basic/Bearer/Digest + `make_authenticate_headers` [FORK] | `security/http.py:84-92,197-219,303-316,404-417` |
| OAuth2 base/PasswordBearer/AuthCodeBearer + `make_not_authenticated_error` | `security/oauth2.py:401-430,536-544,642-650` |
| OAuth2PasswordRequestForm(Strict) | `security/oauth2.py:14-327` |
| SecurityScopes | `security/oauth2.py:653-693` |
| OpenIdConnect | `security/open_id_connect_url.py:74-94` |
| Security OpenAPI models | `openapi/models.py:321-396` |
| OpenAPI/Operation/Info/Parameter/Components field order | `openapi/models.py:73-80,243-261,289-302,399-410,419-431` |
| `get_openapi` top-level | `openapi/utils.py:514-606` |
| `get_openapi_path` per-operation | `openapi/utils.py:260-478` |
| `get_openapi_security_definitions` | `openapi/utils.py:81-104` |
| parameter / requestBody builders | `openapi/utils.py:107-212` |
| 422 const + status_code_ranges | `openapi/utils.py:42-78` |
| REF_PREFIX/TEMPLATE, METHODS_WITH_BODY | `openapi/constants.py` |
| `get_definitions` / `get_schema_from_model_field` / `normalize_name` / bytes override | `_compat/v2.py:60-70,254-282,285-346,425-434` |
| `jsonable_encoder` model→`model_dump(by_alias,exclude_none)` | `encoders.py:243-315` |
| scope accumulation in DAG | `dependencies/models.py:53-103`, `dependencies/utils.py:123-189,305,330-342` |
| `SecurityScopes` injection | `dependencies/utils.py:725-728` |
| security `__call__` invocation (no scope enforcement) | `dependencies/utils.py:660-684` |
| `Body_<unique_id>` wrapper | `dependencies/utils.py:1001-1052` |
| operationId / unique_id | `utils.py:80-99` |
| `is_body_allowed_for_status_code` / `deep_dict_update` | `utils.py` |
| docs endpoint wiring (defaults, root_path) | `applications.py:198-477,1101-1159` |
| Swagger/ReDoc HTML + `_html_safe_json` + oauth2-redirect blob | `openapi/docs.py:9-389` |
| `_SSE_EVENT_SCHEMA` | `sse.py:9` |
| OpenAPI snapshot oracles | `tests/test_security_oauth2.py:149-280`, `tests/test_openapi_separate_input_output_schemas.py:159-416`, `tests/test_duplicate_models_openapi.py:36-84`, `tests/test_openapi_examples.py:150-245`, `tests/test_security_*` |
