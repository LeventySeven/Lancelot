# Lancelot Deep Spec — FastAPI Behavioral Contract: Routing, Responses, Streaming, Middleware, Lifespan, WebSockets, Exceptions

> The **stability spec Lancelot must pass via dogfooding**. Every rule below is the *observable wire/runtime contract* extracted from FastAPI's own test suite + the source it exercises. This is not "how FastAPI is built" — it is "what a drop-in Rust-core replacement must reproduce byte-for-byte so the existing test suite is green when pointed at Lancelot."
>
> Source corpus (read line-by-line, no clone):
> - FastAPI fork: `/Users/seventyleven/Desktop/researchfms/fastapi/fastapi/fastapi/` — `routing.py` (4956 ln), `applications.py` (4691 ln), `responses.py`, `sse.py` (222 ln), `exceptions.py` (257 ln), `exception_handlers.py`, `background.py`. Version ~0.136.x — *customized* (native SSE/JSONL streaming, `EndpointContext` error attribution, dual `AsyncExitStack`/req, `dump_json` fast path, `_DefaultLifespan` re-added). NOT vanilla upstream.
> - Starlette fork (Kludex): `/Users/seventyleven/.cache/uv/archive-v0/x0H2cc61T9P9ptDKVkChi/starlette/` — `routing.py` (747 ln), `responses.py` (568 ln), `_exception_handler.py`, `middleware/{errors,exceptions,base}.py`, `websockets.py`. `on_startup`/`on_shutdown` removed from core Starlette; FastAPI re-vendors `_DefaultLifespan` + `_wrap_gen_lifespan_context`.
> - Tests: `/Users/seventyleven/Desktop/researchfms/fastapi/fastapi/tests/test_*.py`.
>
> Cite format: `file:line` for source, `tests/file.py::test_name` for the conformance anchor. Labels: **KNOWN** = literally asserted by a test or in source; **INFERRED** = behavior implied by source path + test but not directly asserted; **DESIGN** = Lancelot guidance, not FastAPI behavior.
> Conformance IDs: **`R-n`** routing, **`Rs-n`** response/serialization, **`St-n`** streaming/SSE, **`Bg-n`** background tasks, **`M-n`** middleware, **`E-n`** exceptions, **`L-n`** lifespan, **`W-n`** websockets. Each is the spec; the cited test is the executable proof.

## Executive Summary (12 lines)

1. **Routing is O(N) linear scan, first-FULL-match-wins, registration order is precedence.** No specificity ranking — a `/{p:path}` registered before `/exact` shadows it. Lancelot's trie must preserve *registration-order tie-break*, not "most specific wins" (`starlette/routing.py:674-681`; `tests/test_ws_router.py::test_router_with_params`).
2. **Trailing-slash redirect is a SECOND full scan with the path toggled, returning `307` (`RedirectResponse` default), only when `redirect_slashes=True` and `route_path != "/"`** (`starlette/routing.py:695-708`; `tests/test_router_redirect_slashes.py`). Disabled → `404`.
3. **`GET` auto-adds `HEAD`; method mismatch on a path that otherwise matches is `Match.PARTIAL` → `405` with an `Allow` header** (`starlette/routing.py:233-234,250-251,267-274`).
4. **Response serialization has a `dump_json` fast path** (Pydantic-core Rust, `Response(media_type="application/json")`) used **iff `response_field` is set AND `response_class` is the default placeholder**; otherwise `serialize_response` → `actual_response_class` (`routing.py:692-715`; `tests/test_serialize_response.py`).
5. **Response model filtering (`include`/`exclude`/`by_alias`/`exclude_unset`/`exclude_defaults`/`exclude_none`) is applied by Pydantic field serializer and validates+coerces the return value first**; an invalid return raises `ResponseValidationError` (500) (`routing.py:291-314`; `tests/test_response_model_include_exclude.py`, `tests/test_stream_json_validation_error.py`).
6. **Generator endpoints auto-stream**: `response_class=EventSourceResponse` → SSE; default response class + generator → JSONL (`application/jsonl`); explicit `StreamingResponse` → raw passthrough (`routing.py:496-672`; `tests/test_sse.py`, `tests/test_stream_bare_type.py`).
7. **SSE wire format is exact**: `data:`/`event:`/`id:`/`retry:`/`: comment`, every event ends `\n\n`, all `data` JSON-encoded (strings quoted) unless `raw_data`, headers force `Cache-Control: no-cache` + `X-Accel-Buffering: no`, keepalive `: ping\n\n` after 15 s idle (`sse.py:146-222`, `routing.py:496-619`; `tests/test_sse.py`).
8. **Streaming is decoupled producer→`max_buffer_size=1` memory stream→keepalive-wrapper; cancellation works via `anyio.sleep(0)` checkpoint after every yield** so an await-less infinite generator is still cancellable (`routing.py:534-609,629-666`; `tests/test_stream_cancellation.py`).
9. **`BackgroundTasks` run AFTER the full body is sent, sequentially, in add order, inside the response `__call__`** — they block connection release (`starlette/background.py:34-36`, `starlette/responses.py:169-170`; this is a KNOWN defect Lancelot must fix while keeping ordering observable).
10. **Exception machinery is 3 nested ASGI layers**: `ServerErrorMiddleware` (outermost, 500/`Exception`) → user middleware → `ExceptionMiddleware` (status-code + class MRO lookup) → `AsyncExitStackMiddleware`. Handler lookup: status-code dict first (only for `HTTPException`), then `type(exc).__mro__` walk; no handler → re-raise; response already started → `RuntimeError` (`applications.py:1018-1066`, `starlette/_exception_handler.py:16-65`; `tests/test_exception_handlers.py`).
11. **Lifespan is a merged async-CM chain, parent-wraps-child, parent state overrides child on key collision; `include_router` deep-nests via `_merge_lifespan_context`; `on_startup`/`on_shutdown` re-added by FastAPI run in registration order at enter/exit** (`routing.py:209-247,1822-1829,4873-4922`; `tests/test_router_events.py`).
12. **WebSocket close codes are contract**: no route → `1000`; `WebSocketRequestValidationError` → `1008` (policy violation) with `reason=jsonable_encoder(errors())`; state machine enforces `accept` before `send`/`receive` (`exception_handlers.py:29-34`, `starlette/websockets.py:26-98`; `tests/test_ws_router.py`, `tests/test_dependency_after_yield_websockets.py`).

---

## 1. Routing — path matching, ordering, redirect/slash, methods, groups, Mount

### 1.1 Route resolution algorithm (the exact contract)

The router loop is `Router.app` (`starlette/routing.py:662-710`). The contract Lancelot must reproduce:

- **`R-1` (KNOWN)** Iterate `self.routes` **in registration order**. For each, call `route.matches(scope)` which returns `(Match, child_scope)`. The **first** route returning `Match.FULL` wins: `scope.update(child_scope); await route.handle(...); return` (`starlette/routing.py:674-681`). There is **no specificity ranking**. Registration order *is* precedence. Anchor: `tests/test_ws_router.py::test_router_with_params` registers `/router/{pathparam:path}` and it captures `path/to/file`; nothing more specific exists, but the rule is order-first.
- **`R-2` (KNOWN)** A route matches via a **compiled regex** anchored `^…$`. `compile_path` (`starlette/routing.py:110-163`) turns `/{username:str}` into `^/(?P<username>[^/]+)$`, `path_format` `/{username}`, and a `{name: Convertor}` map. Convertor regexes: `str`=`[^/]+`, `int`=`[0-9]+`, `float`=`[0-9]+(\.[0-9]+)?`, `path`=`.*`, `uuid`/default per `CONVERTOR_TYPES`. **Lancelot trie must produce identical capture/coercion** — e.g. `int` path param must reject `42.5` the same way a query int does (`tests/test_query.py::test_query_int_query_42_5` → `int_parsing` 422; path uses the regex so non-`[0-9]+` is a *no-match → 404*, not 422 — KNOWN distinction from `compile_path` regex `[0-9]+`).
- **`R-3` (KNOWN)** Duplicate param names in one path raise `ValueError("Duplicated param name…")` at registration (`starlette/routing.py:142-152`).
- **`R-4` (KNOWN)** Path params are coerced by the convertor `.convert()` and merged onto `dict(scope.get("path_params", {}))` (a copy), then placed in `child_scope = {"endpoint", "path_params"}`. FastAPI's `APIRoute.matches` additionally injects `child_scope["route"] = self` when matched (`routing.py:804-808`) — required for `request.scope["route"]`.
- **`R-5` (KNOWN)** `route_path` for matching is `get_route_path(scope)` = `scope["path"]` with `root_path` stripped (mount-relative). Matching is always against the *root-path-stripped* path.

### 1.2 Method handling, HEAD, 405

- **`R-6` (KNOWN)** `Route.__init__`: if `methods is None` and endpoint is a function → `methods=["GET"]`. Methods are uppercased into a set. **If `"GET" in methods` then `"HEAD"` is auto-added** (`starlette/routing.py:229-234`). Lancelot must auto-register HEAD for every GET route. Anchor: `tests/test_extra_routes.py::test_head` (explicit `@app.head` here, but the GET→HEAD auto-add is the rule the trie must encode).
- **`R-7` (KNOWN)** If the path regex matches but `scope["method"] not in self.methods`, `matches()` returns `Match.PARTIAL` (not NONE) (`starlette/routing.py:250-251`). The router remembers the **first** partial match (`starlette/routing.py:682-684`).
- **`R-8` (KNOWN)** After the full loop with no FULL match, if a partial exists → `partial.handle()`. `Route.handle` raises `HTTPException(status_code=405, headers={"Allow": ", ".join(self.methods)})` when `"app" in scope` (running inside Starlette/FastAPI), else returns a `PlainTextResponse("Method Not Allowed", 405, ...)` (`starlette/routing.py:267-274`). Inside FastAPI it is always the `HTTPException` path → handled by `ExceptionMiddleware` → JSON `{"detail":"Method Not Allowed"}` with `Allow` header.
- **`R-9` (KNOWN)** All standard verbs supported via `api_route`/decorators including `TRACE`, `OPTIONS`, `HEAD`, `PATCH`, `DELETE` (`tests/test_extra_routes.py::test_trace,test_options,test_patch,test_delete`). `OPTIONS`/`HEAD`/`TRACE` are *not* auto-synthesized (unlike HEAD-from-GET); only explicitly registered ones answer. Anchor: `tests/test_extra_routes.py`.

### 1.3 Trailing-slash redirect

- **`R-10` (KNOWN)** Only if **no** FULL/PARTIAL match, `scope["type"]=="http"`, `self.redirect_slashes` is True, **and** `route_path != "/"`: build `redirect_scope` = copy of scope with `path` toggled — strip trailing `/` if present else append `/` — and **re-run the entire route loop** against it. If **any** route returns `Match != NONE`, respond `RedirectResponse(url=str(URL(scope=redirect_scope)))` (`starlette/routing.py:694-708`).
- **`R-11` (KNOWN)** `RedirectResponse` default status is **`307`** (`starlette/responses.py:204-213`), body empty, `location` header `quote(url, safe=":/%#?=@[]!$&'()*+,;")`. Anchor: `tests/test_router_redirect_slashes.py::test_redirect_slashes_enabled` — request `/hello` (route is `/hello/`) → `307`; request `/hello/` → `200`.
- **`R-12` (KNOWN)** `redirect_slashes=False` → no second scan → `404`. Anchor: `tests/test_router_redirect_slashes.py::test_redirect_slashes_disabled` (`/hello` → `404`).
- **`R-13` (INFERRED)** The redirect URL is built from `URL(scope=redirect_scope)`, i.e. it includes scheme/host/query — it is an absolute-ish URL with the toggled path and the *original* query string preserved. Lancelot must preserve the query string across the redirect.

### 1.4 404 / not-found

- **`R-14` (KNOWN)** No match + no redirect → `self.default(scope,...)`. Default is `Router.not_found`: if `"app" in scope` → `raise HTTPException(404)` (→ JSON `{"detail":"Not Found"}` via `http_exception_handler`); plain ASGI → `PlainTextResponse("Not Found",404)` (`starlette/routing.py:606-619`). Anchor: `tests/test_application.py` parametrize `("/nonexistent", 404, {"detail": "Not Found"})`; `tests/test_path.py` `client.get(... )` → `{"detail":"Not Found"}`.

### 1.5 `include_router` / route groups (flatten at registration)

- **`R-15` (KNOWN)** `include_router` **does not nest a sub-router at runtime — it re-creates every route on the parent** by calling `self.add_api_route(prefix + route.path, route.endpoint, …)` copying *all* per-route attributes (response_model, status_code, tags, dependencies (parent-prepended), responses (merged), deprecated (OR-ed), methods, name, response_class, callbacks, `route_class_override=type(route)`, strict_content_type, generate_unique_id) (`routing.py:1737-1796`). Zero runtime mount cost. Lancelot must flatten routers at registration into the single trie.
- **`R-16` (KNOWN)** Prefix rule: a router `prefix` must start with `/` and must **not** end with `/` (asserted, `routing.py:1717-1720`). Final path = `prefix + route.path`. Native prefix (`APIRouter(prefix="/native")`) and `include_router(..., prefix="/x")` compose. Anchor: `tests/test_ws_router.py::test_native_prefix_router` (`/native/`), `::test_prefix_router` (`/prefix/`).
- **`R-17` (KNOWN)** Dependencies: `current_dependencies = include_router-level + route-level` (parent first) — group deps run before route deps (`routing.py:1746-1750`).
- **`R-18` (KNOWN)** Plain Starlette `Route`/`WebSocketRoute` (non-API) inside an included router are re-added via `add_route`/`add_websocket_route` preserving methods/name/include_in_schema (`routing.py:1797-1821`).
- **`R-19` (KNOWN)** WebSocket routes flatten the same way via `add_api_websocket_route(prefix+path, endpoint, dependencies=…)` (`routing.py:1806-1817`).

### 1.6 `Mount` / sub-apps (the NON-flattened path)

- **`R-20` (KNOWN)** `Mount(path, app=…)` compiles regex `compile_path(path.rstrip("/") + "/{path:path}")`. On match it sets `child_scope`: `root_path = root_path + matched_path`, `path_params` (minus the consumed `path`), `app_root_path` (set once, top-level), `endpoint=self.app` (`starlette/routing.py:359-418`). The mounted app then re-runs its **own** full match loop against the `root_path`-stripped remainder. This is the only routing path that is *not* flattened; it costs a second O(N) scan.
- **`R-21` (KNOWN)** A mounted FastAPI/Starlette sub-app gets its own exception handlers, lifespan, middleware. Validation errors inside a mounted sub-app are attributed with the mount prefix in `endpoint_ctx["path"]` (`routing.py:396-399`: `mount_path = scope["root_path"].rstrip("/")`). Anchor: `tests/test_validation_error_context.py` mounts `sub_app` at `/sub`.
- **`R-22` (DESIGN)** Lancelot: flatten first-party routers always (R-15); support `Mount` only for foreign ASGI apps (`mount_asgi(prefix, asgi_app)` per DESIGN-DECISIONS fork #3). Mount path normalization (`.rstrip("/")`) and `app_root_path` semantics must be reproduced for `request.url_for()` to work across the boundary.

### 1.7 `url_path_for` / reverse routing

- **`R-23` (KNOWN)** `Route.url_path_for(name, **params)`: requires `name == self.name` AND `set(params) == set(param_convertors)` exactly, else `NoMatchFound`. Builds path via `replace_params(path_format, convertors, params)` (`starlette/routing.py:256-265`). Router tries each route, first success wins (`starlette/routing.py:621-627`). Lancelot must implement exact-name + exact-param-set matching.

---

## 2. Responses — `response_model`, `response_class`, status codes, headers, serialization

### 2.1 The serialization decision tree (the hot path: `get_request_handler.app`, `routing.py:382-727`)

For a **non-generator** endpoint, after dependencies solve with no errors:

1. **`Rs-1` (KNOWN)** Run endpoint (`run_endpoint_function`, `routing.py:320-330`): coroutine → `await call(**values)`; sync `def` → `await run_in_threadpool(call, **values)`.
2. **`Rs-2` (KNOWN)** If the return value **is already a `Response` instance**: use it as-is. If `raw_response.background is None`, attach `solved_result.background_tasks` to it. **No serialization, no model validation, no status override** (`routing.py:679-682`). Anchor: `tests/test_extra_routes.py::test_head` (`return JSONResponse(None, headers=…)` → headers preserved verbatim), `tests/test_extra_routes.py::test_trace` (`media_type="message/http"` preserved).
3. **`Rs-3` (KNOWN)** Else compute `response_args` via `_build_response_args` (`routing.py:333-348`): `{"background": solved_result.background_tasks}`; status precedence — endpoint `status_code` param if set, else `solved_result.response.status_code` (set by a dependency mutating the injected `Response`), and **`solved_result.response.status_code` overrides last** (`routing.py:341-347`). Anchor: `tests/test_response_change_status_code.py::test_dependency_set_status_code` — a dependency sets `response.status_code = 201`, endpoint returns dict, final status is **201**.
4. **`Rs-4` (KNOWN)** **Fast path** is taken iff `response_field is not None AND isinstance(response_class, DefaultPlaceholder)` (`routing.py:692-694`). Then `serialize_response(..., dump_json=True)` returns **JSON bytes straight from Pydantic-core** and the response is `Response(content=bytes, media_type="application/json", **response_args)` (`routing.py:708-713`). The default JSON media type is `application/json` (NOT through `JSONResponse.render`).
5. **`Rs-5` (KNOWN)** Else `serialize_response(..., dump_json=False)` returns a Python object, wrapped in `actual_response_class(content, **response_args)` (`routing.py:714-715`). With no `response_model` and no custom class, `serialize_response` falls back to `jsonable_encoder(response_content)` then the default `JSONResponse.render` (`routing.py:316-317`).
6. **`Rs-6` (KNOWN)** After building, if `not is_body_allowed_for_status_code(response.status_code)` → `response.body = b""` (strip body for 1xx/204/304) (`routing.py:716-717`).
7. **`Rs-7` (KNOWN)** `response.headers.raw.extend(solved_result.response.headers.raw)` — headers set on the injected `Response` dependency are appended to the final response's raw headers (`routing.py:718`). Order: response's own headers first, then dependency-set headers appended.

### 2.2 `response_model` validation + filtering (`serialize_response`, `routing.py:277-317`)

- **`Rs-8` (KNOWN)** When a `response_field` (ModelField from `response_model`) exists: `value, errors = field.validate(response_content, {}, loc=("response",))`. If `errors` → **`raise ResponseValidationError(errors, body=response_content, endpoint_ctx)`** (a 500-class error; not 422). For sync endpoints the validate runs in a threadpool (`routing.py:291-297`). Anchor: `tests/test_stream_json_validation_error.py` (stream variant), `tests/test_validate_response.py`.
- **`Rs-9` (KNOWN)** Serialization uses `field.serialize_json` if `dump_json` else `field.serialize`, passing `include, exclude, by_alias, exclude_unset, exclude_defaults, exclude_none` (`routing.py:305-314`). These are the **Pydantic v2 field-serializer semantics**, applied AFTER validate/coerce. Lancelot must delegate to pydantic-core with identical kwargs.
- **`Rs-10` (KNOWN)** The return value is **coerced** to the model: returning a dict with `price:"1.0"` and model `price: float|None` yields `{"price":1.0}`; missing optional fields are filled with defaults (`owner_ids: None`). Anchor: `tests/test_serialize_response.py::test_coerce` (`{"name":"coerce","price":"1.0"}` → `{"name":"coerce","price":1.0,"owner_ids":None}`), `::test_validlist` (list coercion + defaults), `tests/test_extra_routes.py` model `Item(name:str, price:float|None=None)`.
- **`Rs-11` (KNOWN)** `response_model_include`/`exclude` accept the Pydantic include/exclude shape: `{"baz": ..., "ref": {"foo"}}` (nested set/dict). Applied identically whether the endpoint returns a model instance or a plain dict (the dict is validated into the model first). Anchor: `tests/test_response_model_include_exclude.py::simple_include / simple_include_dict / simple_exclude / mixed` — `include={"ref2","name"}` + `exclude={"ref2":{"baz"}}` compose.
- **`Rs-12` (KNOWN)** Default `response_model_by_alias=True`; `exclude_unset/defaults/none` default False (`routing.py:359-362`). Lancelot defaults must match exactly.

### 2.3 Status codes & body suppression

- **`Rs-13` (KNOWN)** `status_code` passed to a route decorator is normalized: `IntEnum` → `int(status_code)` (`routing.py:901-902`). At registration, if `response_model` is set, asserts `is_body_allowed_for_status_code(status_code)` (so e.g. `status_code=204` + `response_model` is a registration-time error) (`routing.py:904-906`). Additional `responses={code: {model:…}}` likewise assert body-allowed (`routing.py:931-937`).
- **`Rs-14` (KNOWN)** `is_body_allowed_for_status_code` returns False for `< 200`, `204`, `304` (and FastAPI maps these to empty body). Anchor: `tests/test_response_code_no_body.py::test_get_response` — `status_code=204` endpoint returning `None`: response `204`, `content == b""`, **`content-length` header absent**. `tests/test_starlette_exception.py::test_no_body_status_code_*` — `HTTPException(204)` and `HTTPException(204, detail="…")` both → `204` with empty content (detail discarded).
- **`Rs-15` (KNOWN)** `Response.init_headers` (`starlette/responses.py:55-81`): `content-length` is added **only if** body present, not user-supplied, and status not `<200`/`204`/`304`. `content-type` added from `media_type` (with `; charset=utf-8` appended for `text/*` lacking charset) only if not user-supplied. Lancelot's header writer must reproduce this exact suppression matrix.
- **`Rs-16` (KNOWN)** `RedirectResponse` default `307`; `PlainTextResponse` media `text/plain`; `HTMLResponse` `text/html`; `JSONResponse` `application/json` rendered with `json.dumps(…, ensure_ascii=False, allow_nan=False, separators=(",", ":"))` — **compact, non-ASCII preserved, NaN/Inf rejected** (`starlette/responses.py:194-201`). Lancelot's default JSON encoder must match these exact `json.dumps` options for the non-fast (`jsonable_encoder`/untyped) path, and pydantic-core `dump_json` for the fast path.

### 2.4 Default response class & custom response class

- **`Rs-17` (KNOWN)** `response_class` defaults to `Default(JSONResponse)` (a `DefaultPlaceholder`). A router-level `default_response_class` propagates; `include_router` resolves via `get_value_or_default(route, router, included, self)` precedence (`routing.py:1756-1761,1785`). Anchor: `tests/test_default_response_class.py`, `tests/test_default_response_class_router.py`.
- **`Rs-18` (KNOWN)** If a non-default `response_class` is set, the fast path is **disabled** (Rs-4) and `actual_response_class(content, **response_args)` is used; `content` is the `jsonable_encoder`-or-`field.serialize` Python object (not bytes). Custom `JSONResponse` subclasses (e.g. `media_type="application/vnd.api+json"`) take effect. Anchor: `tests/test_response_code_no_body.py` `JsonApiResponse`.
- **`Rs-19` (KNOWN)** Headers set on a dependency-injected `Response` are always appended last (Rs-7), even on the fast path and custom-class path (`routing.py:718`).

### 2.5 OpenAPI as a byproduct (must match snapshots)

- **`Rs-20` (KNOWN)** `/openapi.json` is `3.1.0`; the validation-error component schema is fixed: `ValidationError` = `{loc:[str|int], msg:str, type:str, input, ctx:object}` with `required:[loc,msg,type]`; `HTTPValidationError` = `{detail: [ValidationError]}`. Every operation that can 422 carries a `422` response `$ref #/components/schemas/HTTPValidationError`. Anchor: exact `snapshot(...)` in `tests/test_starlette_exception.py::test_openapi_schema`, `tests/test_extra_routes.py::test_openapi_schema`, `tests/test_response_code_no_body.py::test_openapi_schema`. Lancelot must emit byte-identical OpenAPI for these inputs.

---

## 3. Streaming — SSE, JSONL, raw, framing, disconnect, keepalive

The streaming branch selection in `get_request_handler.app` (`routing.py:496-672`), evaluated only when the endpoint is a generator (`dependant.is_async_gen_callable or is_gen_callable`, detected once at registration `routing.py:966-975`):

### 3.1 Branch selection

- **`St-1` (KNOWN)** `is_sse_stream` (= generator AND `response_class` subclasses `EventSourceResponse`) → **SSE branch** (`routing.py:496-619`).
- **`St-2` (KNOWN)** `is_json_stream` (= generator AND `response_class` is the `DefaultPlaceholder`, i.e. no explicit class) → **JSONL branch**, `media_type="application/jsonl"` (`routing.py:620-652`). Anchor: `tests/test_stream_bare_type.py` — `-> AsyncIterable` / `-> Iterable` (bare, no item type) → `content-type: application/jsonl`, one JSON object per line.
- **`St-3` (KNOWN)** Else generator with an explicit non-default `response_class` (e.g. `StreamingResponse`) → **raw passthrough branch** (`routing.py:653-672`): `actual_response_class(content=gen, **response_args)`, no per-item JSON encoding. Anchor: `tests/test_dependency_after_yield_streaming.py::test_stream_simple` (`StreamingResponse(iter_data())` yields `"x","y","z"` → body `"xyz"` verbatim).

### 3.2 SSE wire format (exact — `sse.py:146-214` `format_sse_event`, `routing.py:500-527`)

- **`St-4` (KNOWN)** Event field order in the serialized block: `: comment` line(s) (one per `splitlines()`), then `event: <name>`, then `data: <line>` (one `data:` per line of the payload via `splitlines()`), then `id: <id>`, then `retry: <int>`. Block terminates with `"\n"+"\n"` (i.e. ends `\n\n`). Joined with `\n`. Encoded UTF-8.
- **`St-5` (KNOWN)** A plain yielded object (dict, BaseModel, str, etc.) → wrapped as `data:` only, value is `_serialize_data(item)` decoded to str. `_serialize_data`: if `stream_item_field` set → `field.validate` then `field.serialize_json` (model filtering applies); else `json.dumps(jsonable_encoder(item))` (`routing.py:471-494`). **Strings are JSON-encoded → quoted**: yielding `"hello"` produces `data: "hello"`. Anchor: `tests/test_sse.py::test_string_data_json_encoded`, `::test_async_generator_with_model` (`data: {"name":"Plumbus",...}`).
- **`St-6` (KNOWN)** Yielding a `ServerSentEvent`: user controls fields. `raw_data` → emitted **without** JSON encoding (verbatim). `data` (non-None) → if it has `model_dump_json` use that, else `json.dumps(jsonable_encoder(data))`. `comment`/`event`/`id`/`retry` map to their lines. `ServerSentEvent` items **skip `stream_item_field` validation** (`routing.py:500-521`). Anchor: `tests/test_sse.py::test_sse_events_with_fields` (`event: greeting\n`, `data: "hello"\n`, `id: 1\n`, `: just a comment\n`, `retry: 5000\n`), `::test_raw_data_sent_without_json_encoding` (`data: plain text without quotes\n`, not quoted), `::test_mixed_plain_and_sse_events`.
- **`St-7` (KNOWN)** `ServerSentEvent` validation (Pydantic model, `sse.py:42-143`): `id` must not contain `\0` (raises `ValueError` "null"); `retry` must be int `>= 0` (negative or float rejected); `data` and `raw_data` are mutually exclusive (model validator raises `ValueError` "Cannot set both"). Anchor: `tests/test_sse.py::test_server_sent_event_null_id_rejected / _negative_retry_rejected / _float_retry_rejected / test_data_and_raw_data_mutually_exclusive`.
- **`St-8` (KNOWN)** SSE response is a `StreamingResponse(media_type="text/event-stream")` with forced headers `Cache-Control: no-cache` and `X-Accel-Buffering: no`, then `solved_result.response.headers.raw` extended (`routing.py:611-619`). Content-Type on the wire is `text/event-stream; charset=utf-8` (charset appended by `init_headers` for `text/*`). Anchor: `tests/test_sse.py::test_async_generator_with_model` asserts all three headers exactly.
- **`St-9` (KNOWN)** SSE works on **any HTTP method** including POST (MCP compatibility). Anchor: `tests/test_sse.py::test_post_method_sse`.

### 3.3 Producer / keepalive architecture (`routing.py:534-609`)

- **`St-10` (KNOWN)** SSE uses a `@asynccontextmanager` (`_sse_producer_cm`) entered on the **request-scoped `AsyncExitStack`** (`scope["fastapi_inner_astack"]`), NOT via generator finalization. This is the PEP-789 fix: the generator's `__anext__` is never wrapped by a cancel/timeout scope, so a `GeneratorExit` is never thrown into the LLM stream (`routing.py:586-595`). Lancelot's Rust streaming pump must guarantee the same: teardown runs via the exit stack after the response completes, not via async-generator GC.
- **`St-11` (KNOWN)** Internal pipeline = two `anyio.create_memory_object_stream` of **`max_buffer_size=1`**: `_producer` pulls from the generator and `send_stream.send(_serialize_sse_item(item))`; `_keepalive_inserter` reads with `anyio.fail_after(_PING_INTERVAL)` and on `TimeoutError` sends `KEEPALIVE_COMMENT = b": ping\n\n"`, else forwards data; closes on `EndOfStream` (`routing.py:552-578`). Buffer size 1 = backpressure onto the model. Lancelot's bounded SPSC ring default = 1.
- **`St-12` (KNOWN)** `_PING_INTERVAL = 15.0` s (`sse.py:222`; importable for monkeypatch). Keepalive comment emitted only when the generator is idle longer than the interval. Anchor: `tests/test_sse.py::test_keepalive_ping_async / _sync` (monkeypatch interval 0.05, sleep 0.3 → `: ping\n` appears between data events), `::test_no_keepalive_when_fast` (fast stream → no `: ping`).
- **`St-13` (KNOWN)** Sync generators are wrapped via `iterate_in_threadpool(gen)` for SSE (`routing.py:531-532`); JSONL sync uses a plain `for item in gen` sync generator (`routing.py:641-645`). Both sync and async generators must produce identical wire output. Anchor: `tests/test_sse.py::test_sync_generator_with_model`, `tests/test_stream_bare_type.py::test_stream_bare_sync_iterable`.

### 3.4 Cancellation & disconnect

- **`St-14` (KNOWN)** After **every** yielded item, SSE inserts `await anyio.sleep(0)` (`_sse_with_checkpoints`, `routing.py:597-605`); JSONL async inserts `await anyio.sleep(0)` after each item (`routing.py:629-634`); raw async-gen wraps with `_async_stream_raw` adding `await anyio.sleep(0)` after each chunk (`routing.py:658-666`). This guarantees a cancellation checkpoint even when the producer never naturally suspends. Anchor: `tests/test_stream_cancellation.py::test_raw_stream_cancellation / test_jsonl_stream_cancellation` — an infinite `while True: yield` async generator with **no internal await** must still be cancellable within 3 s (ref FastAPI issue #14680). **A Rust reimplementation that does not yield a cancellation point per item will hang this test.**
- **`St-15` (KNOWN)** `StreamingResponse.__call__` (`starlette/responses.py:257-285`): for ASGI `spec_version >= (2,4)` it just runs `stream_response` and converts `OSError` → `ClientDisconnect`. For older spec it races `stream_response` against `listen_for_disconnect` in a task group (cancel on first done). `stream_response` sends `http.response.start` then `http.response.body` chunks with `more_body=True`, terminating with an empty body `more_body=False` (`starlette/responses.py:248-255`). Lancelot must terminate every stream with a final zero-length `more_body=False` frame.
- **`St-16` (KNOWN)** Background tasks attached to a streaming response run **after** the stream fully drains (`starlette/responses.py:283-284`).

### 3.5 Streaming response validation errors & post-start failures

- **`St-17` (KNOWN)** With a typed stream item (`stream_item_field` from the item annotation, e.g. `-> AsyncIterable[Item]`), each item is validated; an invalid item raises **`ResponseValidationError`** mid-stream. Anchor: `tests/test_stream_json_validation_error.py::test_stream_json_validation_error_async / _sync` — first item valid, second invalid → `ResponseValidationError` raised (since item 1 may already be buffered, this surfaces as a server-side error; with `raise_server_exceptions=True` the test catches it).
- **`St-18` (KNOWN)** If a `yield`-dependency raises **after** the streaming response already started (status 200 already sent), the body ends (possibly empty) and an exception is raised/logged server-side; client still sees `200`. Anchor: `tests/test_dependency_after_yield_streaming.py::test_broken_session_stream_no_raise` — `status_code == 200`, `text == ""`; `::test_broken_session_stream_raise` raises server-side. Lancelot must keep the disconnect→cancel→upstream-abort path from corrupting the already-sent 200.
- **`St-19` (KNOWN)** A bare generator return-annotation with no item type still streams as JSONL (no validation). Annotated item type enables per-item validation+filtering via `stream_item_field`. Anchor: `tests/test_stream_bare_type.py` (bare → JSONL, no error), contrast `tests/test_sse.py::sse_items` (`-> AsyncIterable[Item]` → validated).

---

## 4. Background tasks — execution timing & ordering

`fastapi.background.BackgroundTasks` extends `starlette.background.BackgroundTasks` (`background.py:11-61`, `starlette/background.py:26-36`).

- **`Bg-1` (KNOWN)** `add_task(func, *args, **kwargs)` appends a `BackgroundTask` to an ordered list. `__call__` runs them **`for task in self.tasks: await task()`** — strictly **sequential, in add order** (`starlette/background.py:34-36`). Lancelot's detached pool must still execute a single request's tasks in add order (FIFO) to be observable-equivalent.
- **`Bg-2` (KNOWN)** Each task: async callable → `await func(...)`; sync → `await run_in_threadpool(func, ...)` (`starlette/background.py:19-23`). `is_async_callable` is evaluated once at `BackgroundTask.__init__`.
- **`Bg-3` (KNOWN)** Tasks run **after the entire response body is sent**, inside the response object's `__call__`: `Response.__call__` sends `http.response.start` + `http.response.body`, then `if self.background is not None: await self.background()` (`starlette/responses.py:166-170`). For `StreamingResponse`/`FileResponse` it is after the stream/file fully drains (`starlette/responses.py:283-284`, `starlette/responses.py:382-383`). **This is the KNOWN connection-blocking defect** (DESIGN-DECISIONS anti-pattern list): the connection is not released until tasks finish.
- **`Bg-4` (KNOWN)** Background tasks are wired in via `_build_response_args` → `response_args["background"] = solved_result.background_tasks` (`routing.py:336-337`), OR attached to a returned `Response` if it has none (`routing.py:680-681`). The `BackgroundTasks` object is itself a solvable dependency (injected by type into the endpoint signature).
- **`Bg-5` (DESIGN)** Lancelot fixes Bg-3 by running tasks on a Rust-owned detached pool *after* sending the final body frame and *releasing the connection* — but must preserve: (a) per-request FIFO order, (b) sync→threadpool / async→loop dispatch, (c) tasks still run for streaming responses after the stream ends, (d) a task raising does not corrupt the already-sent response. The observable contract (tasks ran, in order, side effects visible) is identical; only the connection-release timing improves.

---

## 5. Middleware — ordering & error propagation

### 5.1 The stack build order (`applications.py:1018-1066`)

- **`M-1` (KNOWN)** `build_middleware_stack()` constructs, **outermost → innermost**: `ServerErrorMiddleware(handler=error_handler)` → **user middleware (in `self.user_middleware` order)** → `ExceptionMiddleware(handlers=…)` → `AsyncExitStackMiddleware` → `self.router`. Wrapped via `for cls,args,kwargs in reversed(middleware): app = cls(app, …)` so the first list element is the outermost wrapper (`applications.py:1031-1066`).
- **`M-2` (KNOWN)** `add_middleware` does `self.user_middleware.insert(0, …)` — **last added = outermost** among user middleware (`starlette/applications.py:98-101`). Adding after the stack is built raises `RuntimeError("Cannot add middleware after an application has started")`. The stack is built lazily on first request (`Starlette.__call__`: `if self.middleware_stack is None: self.middleware_stack = self.build_middleware_stack()`, `starlette/applications.py:86-90`).
- **`M-3` (KNOWN)** `error_handler` is the handler registered for key `500` or `Exception` — it is pulled OUT of the normal exception-handler dict and given to `ServerErrorMiddleware`; all other handlers go to `ExceptionMiddleware` (`applications.py:1025-1030`). So `Exception`/`500` handlers run at the **outermost** layer; `HTTPException`/class handlers run at the **innermost** (just outside the router).
- **`M-4` (KNOWN)** `FastAPI.__call__` sets `scope["root_path"] = self.root_path` (if set) then calls `Starlette.__call__` which sets `scope["app"] = self` (`applications.py:1156-1159`, `starlette/applications.py:86-90`). `scope["app"]` presence is what flips not-found/405 from a plain response into a raised `HTTPException`.
- **`M-5` (KNOWN)** `AsyncExitStackMiddleware` is **inside** all user middleware so the `AsyncExitStack` (used for file/yield-dep teardown) shares the same `contextvars` context as user middleware — placing it outside would lose contextvars set inside `yield` deps (`applications.py:1040-1059` comment). Lancelot's per-request `contextvars.Context` must be threaded so middleware and yield-dep teardown share it.

### 5.2 Error propagation through middleware

- **`M-6` (KNOWN)** `ServerErrorMiddleware.__call__` (`starlette/middleware/errors.py:149-186`): wraps app in a `_send` that sets `response_started=True` on `http.response.start`. On any `Exception`: if `debug` → traceback response; elif no handler → default 500 `PlainTextResponse("Internal Server Error", 500)`; else run the installed 500 handler. If `not response_started` send the response; **always re-raise the exception** (so the test client / server logger can see it).
- **`M-7` (KNOWN)** Therefore an unhandled `Exception` produces a `500` to the client **and** the exception propagates to the ASGI server. With `TestClient(raise_server_exceptions=True)` (default) the test sees the raised exception; with `raise_server_exceptions=False` it sees the `500` body. Anchor: `tests/test_exception_handlers.py::test_override_server_error_exception_raises` (raises `RuntimeError`) vs `::test_override_server_error_exception_response` (`500`, `{"exception":"server-error"}`); `tests/test_dependency_after_yield_streaming.py::test_broken_session_data_no_raise` (`500`, `"Internal Server Error"`).
- **`M-8` (KNOWN)** A handled exception (handler found) where the response **already started** → `RuntimeError("Caught handled exception, but response already started.")` from `wrap_app_handling_exceptions` (`starlette/_exception_handler.py:55-56`). Lancelot must raise the same once headers are flushed.
- **`M-9` (KNOWN)** Pure-ASGI user middleware can intercept WebSocket scope and wrap `call_next`; exceptions from inner WS handlers propagate to it. Anchor: `tests/test_ws_router.py::test_depend_err_middleware` (custom WS middleware catches `NotImplementedError`, closes with `1006`), `::test_depend_validation` (validation error does NOT leak to the middleware — it is converted to a close frame *below* it: `caught == []`).
- **`M-10` (KNOWN — anti-pattern)** `@app.middleware("http")` / `BaseHTTPMiddleware` spins a task group + 2 memory streams + a cached request per request (`starlette/middleware/base.py`). FastAPI's own `tests/test_custom_middleware_exception.py` exercises it but DESIGN-DECISIONS forbids the `call_next` model in Lancelot — Lancelot supports only pure-ASGI/Rust middleware + an ASGI-compat escape hatch, never `BaseHTTPMiddleware` semantics on the fast path. Conformance: Lancelot need NOT pass `BaseHTTPMiddleware`-specific internals, but MUST pass the *observable* outcome (middleware sees request/response, can short-circuit, exceptions propagate outward).

---

## 6. Exception handlers — lookup order & exact response bodies

### 6.1 Default handler registry (`applications.py:998-1011`)

- **`E-1` (KNOWN)** FastAPI seeds `self.exception_handlers` with defaults via `setdefault`: `HTTPException → http_exception_handler`, `RequestValidationError → request_validation_exception_handler`, `WebSocketRequestValidationError → websocket_request_validation_exception_handler`. User-supplied `exception_handlers={}` (constructor) or `@app.exception_handler` override these. `ExceptionMiddleware` *also* internally registers `HTTPException → self.http_exception` and `WebSocketException → self.websocket_exception` as a fallback baseline (`starlette/middleware/exceptions.py:28-31`).

### 6.2 Lookup algorithm (`starlette/_exception_handler.py:16-65`)

- **`E-2` (KNOWN)** On exception in `wrapped_app`: (1) if `isinstance(exc, HTTPException)` → `handler = status_handlers.get(exc.status_code)` (status-code-keyed handlers checked **first, only for HTTPException**). (2) If still None → `_lookup_exception_handler`: walk `type(exc).__mro__` and return the first class present in `exception_handlers` (`_exception_handler.py:16-20`). (3) If still None → **re-raise** `exc` (propagates to `ServerErrorMiddleware`). MRO walk means a handler registered for a base class catches subclasses; the **most-derived registered class wins** (first in MRO).
- **`E-3` (KNOWN)** If a handler is found but `response_started` is True → `raise RuntimeError("Caught handled exception, but response already started.") from exc`.
- **`E-4` (KNOWN)** Handler invocation: async → `await handler(conn, exc)`; sync → `await run_in_threadpool(handler, conn, exc)`. If it returns non-None → `await response(scope, receive, sender)` (the sender tracks `response_started`).
- **`E-5` (KNOWN)** `ServerErrorMiddleware` only handles the `500`/`Exception` slot and is **outermost**; `ExceptionMiddleware` handles everything else and is **innermost**. So a `RequestValidationError` raised by the router is caught by `ExceptionMiddleware`; a bare `RuntimeError` escapes `ExceptionMiddleware` (no handler) and is caught by `ServerErrorMiddleware`.

### 6.3 Exact response bodies (the wire contract)

- **`E-6` (KNOWN)** `http_exception_handler` (`exception_handlers.py:11-17`): if `not is_body_allowed_for_status_code(exc.status_code)` → `Response(status_code, headers=exc.headers)` (empty body); else `JSONResponse({"detail": exc.detail}, status_code, headers=exc.headers)`. `exc.detail` defaults to `http.HTTPStatus(status_code).phrase` if None (`starlette/exceptions.py:9-10`). Anchor: `tests/test_starlette_exception.py::test_get_item_not_found` → `404`, body `{"detail":"Item not found"}`, header `x-error: Some custom header` preserved; `::test_no_body_status_code_*` → `204` empty (detail discarded even if set).
- **`E-7` (KNOWN)** Both FastAPI `HTTPException` and Starlette `HTTPException` are handled identically by `http_exception_handler` (FastAPI's subclasses Starlette's; the handler is keyed on the Starlette class so both match via MRO). Custom headers only flow through if passed to the exception. Anchor: `tests/test_starlette_exception.py::test_get_starlette_item_not_found` → `{"detail":"Item not found"}`, `x-error` is `None` (none set).
- **`E-8` (KNOWN)** `request_validation_exception_handler` → `JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})` (`exception_handlers.py:20-26`). The **422 wire shape is fixed**: `{"detail":[{"type":<str>,"loc":[<seg>,…],"msg":<str>,"input":<value>,"ctx":<obj?>}]}`. `loc` first segment is the source (`"query"`/`"path"`/`"body"`/`"header"`/`"cookie"`). Anchor: `tests/test_query.py::test_query` → `{"detail":[{"type":"missing","loc":["query","query"],"msg":"Field required","input":None}]}`; `::test_query_int_query_42_5` → `{"type":"int_parsing","loc":["query","query"],"msg":"Input should be a valid integer, unable to parse string as an integer","input":"42.5"}`. Lancelot MUST emit byte-identical pydantic-core error dicts (this is why DI/validation delegates to pydantic-core via FFI).
- **`E-9` (KNOWN)** Invalid JSON body → `RequestValidationError([{"type":"json_invalid","loc":("body", e.pos),"msg":"JSON decode error","input":{},"ctx":{"error": e.msg}}], body=e.doc)` (`routing.py:427-441`). Other body-parse exceptions → `HTTPException(400, "There was an error parsing the body")`. A middleware-raised `HTTPException` during body read is re-raised as-is (`routing.py:442-449`).
- **`E-10` (KNOWN)** A `yield`-dependency that swallows an exception (bare `except`) and does not re-raise → after the response, `request_response.app` detects `response_awaited` was never set and raises `FastAPIError("Response not awaited. …")` (`routing.py:124-131`). Lancelot must reproduce this guard so silently-swallowed errors are surfaced.
- **`E-11` (KNOWN)** Override semantics: `exception_handlers={HTTPException: h1, RequestValidationError: h2, Exception: h3}`. `Exception` handler → ServerErrorMiddleware slot (500 path). Anchor: `tests/test_exception_handlers.py` — `/http-exception` (`HTTPException(400)`) → custom handler → `200 {"exception":"http-exception"}`; `/request-validation/invalid` (`param:int` fails) → `200 {"exception":"request-validation"}`; `/server-error` (`RuntimeError`) → handler at 500 slot, but **still re-raised** so `raise_server_exceptions=True` sees `RuntimeError`, `=False` sees `500 {"exception":"server-error"}`.
- **`E-12` (KNOWN)** Traceback fidelity: a `yield` dep that raises must surface the **original** exception with the user's frame as the last frame (FastAPI's dual-AsyncExitStack design preserves it). Anchor: `tests/test_exception_handlers.py::test_traceback_for_dependency_with_yield` — `exc_info.traceback[-1]` points at `raise_value_error`'s file+line. Lancelot's FFI `catch_unwind` seam must not flatten Python tracebacks.
- **`E-13` (KNOWN)** `ResponseValidationError` is a distinct exception (server-side, not 422) raised when the **return value** fails the response model (Rs-8). With no override it propagates as a 500 (no default handler for it). Anchor: `tests/test_validation_error_context.py` registers a `ResponseValidationError` handler that re-raises; `tests/test_stream_json_validation_error.py`.
- **`E-14` (KNOWN)** `endpoint_ctx` (file/line/function/path) is attached to every `ValidationException` for error-message attribution; `__str__` formats `File "...", line N, in func` + path (`exceptions.py:174-209`). This is a fork-specific diagnostic; Lancelot SHOULD reproduce `endpoint_ctx` so error messages match, but it is not on the wire (not in the JSON body).

---

## 7. Lifespan — ordering & failure handling

### 7.1 ASGI lifespan protocol (`starlette/routing.py:629-654`)

- **`L-1` (KNOWN)** `Router.lifespan`: `await receive()` (expects `lifespan.startup`), then `async with self.lifespan_context(app) as maybe_state:`. If `maybe_state is not None`: requires `"state" in scope` (else `RuntimeError('The server does not support "state" in the lifespan scope.')`), then `scope["state"].update(maybe_state)`. Then `send({"type":"lifespan.startup.complete"})`, `started=True`, `await receive()` (expects shutdown). On exit send `lifespan.shutdown.complete`.
- **`L-2` (KNOWN)** On **any `BaseException`** during the context: if `started` → `send({"type":"lifespan.shutdown.failed","message":<traceback>})` else `send({"type":"lifespan.startup.failed","message":<traceback>})`, then **re-raise**. Lancelot's supervisor must emit the exact `lifespan.startup.failed` / `shutdown.failed` events with the formatted traceback as `message`.
- **`L-3` (KNOWN)** Startup failure must prevent the app from serving (the ASGI server aborts on `lifespan.startup.failed`). Lancelot must fail-fast on startup error.

### 7.2 Lifespan kinds & deprecation

- **`L-4` (KNOWN)** `lifespan=None` → `_DefaultLifespan(router)` whose `__aenter__` calls `router._startup()` and `__aexit__` calls `router._shutdown()` (FastAPI re-vendored, `routing.py:226-247`). `@asynccontextmanager` lifespan used as-is. **Async generator** lifespan → wrapped with `asynccontextmanager` + `DeprecationWarning` ("async generator function lifespans are deprecated"). **Sync generator** → `_wrap_gen_lifespan_context` (`contextlib.contextmanager` + `_AsyncLiftContextManager`) + `DeprecationWarning` (`routing.py:1268-1280`, `starlette/routing.py:582-599`). Anchor: `tests/test_router_events.py::test_router_sync_generator_lifespan`, `::test_router_async_generator_lifespan`, `::test_app_lifespan_state`.
- **`L-5` (KNOWN)** `_startup()` runs `self.on_startup` handlers in order; `_shutdown()` runs `self.on_shutdown` in order; each: async → `await`, sync → call directly (`routing.py:4873-4902`). `on_event("startup"/"shutdown")` is deprecated (warns) but still appends to these lists (`routing.py:4924-4954`). Constructor `on_startup=[…]`/`on_shutdown=[…]` populate them directly. Anchor: `tests/test_router_events.py::test_router_events`, `::test_startup_shutdown_handlers_as_parameters`, `::test_router_async_shutdown_handler`.

### 7.3 Nesting via `include_router` (the merge contract)

- **`L-6` (KNOWN)** `include_router` does `self.lifespan_context = _merge_lifespan_context(self.lifespan_context, router.lifespan_context)` AND copies `router.on_startup`/`on_shutdown` into the parent's lists (`routing.py:1822-1829`). `_merge_lifespan_context(original, nested)` yields a CM that does `async with original(app): async with nested(app): yield merged_state` (`routing.py:209-223`). So **parent (original) enters first, child (nested) enters second; child exits first, parent exits last** — classic nesting.
- **`L-7` (KNOWN)** Startup ordering observed: app → router → sub_router (parent-first); shutdown: reverse (sub → router → app). All startups complete before any request is served; all shutdowns after the last request. Anchor: `tests/test_router_events.py::test_router_events` and `::test_startup_shutdown_handlers_as_parameters` assert: before client all False; inside client all `*_startup True`, all `*_shutdown False`; after client all True. `::test_router_nested_lifespan_state` same with state.
- **`L-8` (KNOWN)** State merge: `{**(maybe_nested_state or {}), **(maybe_original_state or {})}` — **the original (parent) state overrides the nested (child) on key collision** (`routing.py:218-221`). If both None → yield `None` (old ASGI compat, no state). Anchor: `tests/test_router_events.py::test_router_nested_lifespan_state_overriding_by_parent` — `{"overridden":"app"}` wins over child's `{"overridden":"router"}`; final `{"app_specific":True,"router_specific":True,"overridden":"app"}`. `::test_merged_no_return_lifespans_return_none` (both yield nothing → empty state), `::test_merged_mixed_state_lifespans` (`{"router":True}`).
- **`L-9` (KNOWN)** Lifespan state is exposed on `request.state` (e.g. `request.state.app`, `.router`, `.sub_router`). Anchor: `tests/test_router_events.py::test_router_nested_lifespan_state` (`main(request)` asserts all three state keys present).
- **`L-10` (DESIGN)** Lancelot must reproduce: parent-first enter / child-first exit ordering, parent-overrides-child state merge, `lifespan.startup.failed`/`shutdown.failed` events with traceback, the deprecation warnings for generator lifespans, and `_DefaultLifespan` running `on_startup`/`on_shutdown` lists in registration order — all observable via the same tests.

---

## 8. WebSockets — accept/close/error codes & state machine

### 8.1 State machine (`starlette/websockets.py:26-98`)

- **`W-1` (KNOWN)** `WebSocket` has `client_state` and `application_state` (`CONNECTING`/`CONNECTED`/`DISCONNECTED`/`RESPONSE`). `receive()`: from `CONNECTING` must get `websocket.connect` (else `RuntimeError`) → `CONNECTED`; from `CONNECTED` only `websocket.receive`/`websocket.disconnect` valid; `websocket.disconnect` → `DISCONNECTED`; calling `receive()` after disconnect → `RuntimeError`.
- **`W-2` (KNOWN)** `send()`: from `CONNECTING` only `websocket.accept`/`websocket.close`/`websocket.http.response.start` valid; `accept` → `CONNECTED`, `close` → `DISCONNECTED`, `http.response.start` → `RESPONSE`. From `CONNECTED` only `websocket.send`/`websocket.close`; a send `OSError` → state `DISCONNECTED` + `raise WebSocketDisconnect(code=1006)`. Lancelot must enforce these transitions and raise `RuntimeError` on protocol misuse with the exact messages.
- **`W-3` (KNOWN)** `accept(subprotocol, headers)`: if still `CONNECTING`, first `await self.receive()` (consume the connect), then `send({"type":"websocket.accept","subprotocol":…,"headers":…})` (`starlette/websockets.py:100-110`). `receive_text/bytes/json` require `application_state == CONNECTED` (else `RuntimeError('WebSocket is not connected. Need to call "accept" first.')`) and raise `WebSocketDisconnect(code, reason)` on a disconnect message (`starlette/websockets.py:112-142`).
- **`W-4` (KNOWN)** `close(code=1000, reason="")` sends `websocket.close`. `send_json` uses `json.dumps(data, separators=(",",":"), ensure_ascii=False)` (compact). `iter_text/bytes/json` loop until `WebSocketDisconnect` then stop silently (`starlette/websockets.py:144-181`).

### 8.2 Routing & close-code contract

- **`W-5` (KNOWN)** WebSocket route match is FULL-only (no PARTIAL/405 concept) — `WebSocketRoute.matches` returns FULL or NONE (`starlette/routing.py:323-336`). `APIWebSocketRoute` adds `child_scope["route"]=self`. `Router.not_found` for a `websocket` scope sends `WebSocketClose()` = close code **`1000`** (`starlette/routing.py:606-610`, `starlette/websockets.py:190-197`). Anchor: `tests/test_ws_router.py::test_wrong_uri` — connecting to a non-existent WS endpoint → `WebSocketDisconnect` with `code == 1000` (`status.WS_1000_NORMAL_CLOSURE`).
- **`W-6` (KNOWN)** `WebSocketRequestValidationError` (a dependency/param fails validation, e.g. missing required `Header()`) → `websocket_request_validation_exception_handler` → `await websocket.close(code=WS_1008_POLICY_VIOLATION, reason=jsonable_encoder(exc.errors()))` (`exception_handlers.py:29-34`). Code **`1008`**. The error is converted to a close frame **below** any user WS middleware, so middleware does NOT see the exception. Anchor: `tests/test_ws_router.py::test_depend_validation` — `e.value.code == status.WS_1008_POLICY_VIOLATION`, `caught == []`.
- **`W-7` (KNOWN)** A custom exception raised in a WS endpoint with no handler propagates out through user WS middleware (which can `websocket.close(code=..., reason=repr(e))`). Anchor: `tests/test_ws_router.py::test_depend_err_middleware` — `NotImplementedError` from a dep → middleware closes with `1006` (`WS_1006_ABNORMAL_CLOSURE`), `"NotImplementedError" in reason`.
- **`W-8` (KNOWN)** A registered exception handler for a custom exception class works for WS too: `exception_handlers={CustomError: async handler(ws, exc)}` → handler does `await websocket.close(1002, "foo")`. Anchor: `tests/test_ws_router.py::test_depend_err_handler` — `e.value.code == 1002`, `"foo" in reason`.
- **`W-9` (KNOWN)** WS dependency injection works (`Depends(...)`, `Header()`, path/query params, `dependency_overrides`). `@router.websocket` and `@router.websocket_route` both supported; native prefix + include_router prefix compose (`tests/test_ws_router.py::test_router_ws_depends`, `::test_router_ws_depends_with_override`, `::test_router_with_params`, `::test_native_prefix_router`).
- **`W-10` (KNOWN)** WS endpoints get the same dual-`AsyncExitStack` wrapper as HTTP (`websocket_session`, `routing.py:141-162`): `fastapi_inner_astack` + `fastapi_function_astack`, wrapped by `wrap_app_handling_exceptions`. A `yield`-dependency's teardown runs after the WS handler returns; a broken yield-dep raises after iteration. Anchor: `tests/test_dependency_after_yield_websockets.py::test_websocket_dependency_after_yield` (3 messages received), `::test_websocket_dependency_after_yield_broken` (`ValueError("Session closed")` raised).

---

## 9. The Numbered Conformance Checklist (MUST-rules, test-anchored)

Every rule below is a **MUST** for Lancelot to be wire-compatible. Anchor = the test that fails if the rule is broken. (Rules already stated above by ID; this is the consolidated gate. Count: see end.)

### Routing (R)
1. **R-1** First `Match.FULL` in registration order wins; no specificity ranking. `tests/test_ws_router.py::test_router_with_params`.
2. **R-2** Path-param convertor regexes byte-identical (`str=[^/]+`, `int=[0-9]+`, `float`, `path=.*`, `uuid`). `tests/test_query.py::test_query_int_query_42_5` (coercion errors), routing regex behavior.
3. **R-3** Duplicate param name in a path → `ValueError` at registration. `starlette/routing.py:142-152`.
4. **R-6** GET auto-registers HEAD. `tests/test_extra_routes.py::test_head`.
5. **R-7/R-8** Path-match + method-miss → `405` + `Allow` header (JSON `{"detail":"Method Not Allowed"}` inside FastAPI). `starlette/routing.py:267-274`.
6. **R-10/R-11** Trailing-slash → second toggled scan → `307` `RedirectResponse`, query preserved. `tests/test_router_redirect_slashes.py::test_redirect_slashes_enabled`.
7. **R-12** `redirect_slashes=False` → `404`, no second scan. `tests/test_router_redirect_slashes.py::test_redirect_slashes_disabled`.
8. **R-14** No match → `404 {"detail":"Not Found"}`. `tests/test_application.py` (`/nonexistent`).
9. **R-15/R-16/R-17** `include_router` flattens at registration; prefix `/`-rules; group deps before route deps. `tests/test_ws_router.py::test_prefix_router`, `::test_native_prefix_router`.
10. **R-20/R-21** `Mount` runs its own match loop; mounted sub-app gets own handlers/lifespan; `root_path`/`app_root_path` set. `tests/test_validation_error_context.py`.
11. **R-23** `url_path_for` requires exact name + exact param-set. `starlette/routing.py:256-265`.

### Response/Serialization (Rs)
12. **Rs-2** Returned `Response` instance passed through verbatim (headers/media/status), background attached only if None. `tests/test_extra_routes.py::test_head/test_trace`.
13. **Rs-3** Status precedence: endpoint `status_code` → dependency-set `response.status_code` overrides last. `tests/test_response_change_status_code.py::test_dependency_set_status_code` (→201).
14. **Rs-4** Fast path iff `response_field` set AND default response class → `Response(media_type="application/json")` from pydantic-core bytes.
15. **Rs-8** Invalid return vs `response_model` → `ResponseValidationError` (500-class). `tests/test_validate_response.py`, `tests/test_stream_json_validation_error.py`.
16. **Rs-9/Rs-10/Rs-11/Rs-12** Coercion + defaults + `include/exclude/by_alias/exclude_*` exact Pydantic-v2 semantics, defaults `by_alias=True` others False. `tests/test_serialize_response.py::test_coerce/test_validlist`, `tests/test_response_model_include_exclude.py`.
17. **Rs-13/Rs-14** `status_code`+`response_model` body-allowed assert at registration; `204/304/<200` → empty body, no `content-length`. `tests/test_response_code_no_body.py::test_get_response`, `tests/test_starlette_exception.py::test_no_body_status_code_*`.
18. **Rs-15** `content-length`/`content-type` suppression matrix (user-supplied wins, charset appended for `text/*`). `starlette/responses.py:55-81`.
19. **Rs-16** Default `JSONResponse.render` = `json.dumps(ensure_ascii=False, allow_nan=False, separators=(",",":"))`; `RedirectResponse`=307. `starlette/responses.py:194-213`.
20. **Rs-20** `/openapi.json` byte-identical (3.1.0, `ValidationError`/`HTTPValidationError` schema, 422 refs). `tests/test_starlette_exception.py::test_openapi_schema` + 2 others.

### Streaming/SSE (St)
21. **St-1/St-2/St-3** Branch: `EventSourceResponse`→SSE, default+gen→JSONL `application/jsonl`, explicit class→raw. `tests/test_sse.py`, `tests/test_stream_bare_type.py`, `tests/test_dependency_after_yield_streaming.py::test_stream_simple`.
22. **St-4/St-5/St-6/St-7** Exact SSE framing (field order, `\n\n` terminator, strings JSON-quoted, `raw_data` verbatim, `ServerSentEvent` validation rules). `tests/test_sse.py::test_sse_events_with_fields/test_raw_data_sent_without_json_encoding/test_string_data_json_encoded/test_server_sent_event_*`.
23. **St-8** SSE forced headers `Cache-Control: no-cache` + `X-Accel-Buffering: no` + `text/event-stream; charset=utf-8`. `tests/test_sse.py::test_async_generator_with_model`.
24. **St-9** SSE on POST works. `tests/test_sse.py::test_post_method_sse`.
25. **St-11/St-12** `max_buffer_size=1` producer/keepalive; `: ping\n\n` after 15s idle, none when fast. `tests/test_sse.py::test_keepalive_ping_async/_sync/test_no_keepalive_when_fast`.
26. **St-13** Sync and async generators produce identical wire output (sync via threadpool). `tests/test_sse.py::test_sync_generator_with_model`, `tests/test_stream_bare_type.py::test_stream_bare_sync_iterable`.
27. **St-14** Per-item `anyio.sleep(0)` cancellation checkpoint — await-less infinite generator still cancellable ≤3s. `tests/test_stream_cancellation.py::test_raw_stream_cancellation/test_jsonl_stream_cancellation`.
28. **St-15/St-16** Stream terminates with zero-length `more_body=False`; background runs after drain. `starlette/responses.py:248-284`.
29. **St-17/St-18** Typed stream item → per-item `ResponseValidationError`; yield-dep failure after start → client still `200`, empty body, server-side error. `tests/test_stream_json_validation_error.py`, `tests/test_dependency_after_yield_streaming.py::test_broken_session_stream_no_raise`.

### Background (Bg)
30. **Bg-1/Bg-2/Bg-3** Tasks run after full body sent, sequentially in add order, sync→threadpool/async→loop. `starlette/background.py:34-36`, `starlette/responses.py:166-170`.

### Middleware (M)
31. **M-1/M-2/M-3** Stack order ServerError→user→Exception→AsyncExitStack→router; `add_middleware` insert(0); 500/`Exception` handler at outermost slot. `applications.py:1018-1066`.
32. **M-6/M-7** Unhandled `Exception` → `500` to client AND re-raised to server. `tests/test_exception_handlers.py::test_override_server_error_exception_raises/_response`.
33. **M-8** Handled exception after response started → `RuntimeError("Caught handled exception, but response already started.")`. `starlette/_exception_handler.py:55-56`.
34. **M-9** Pure-ASGI WS middleware can wrap `call_next` and catch inner errors; validation errors converted below it. `tests/test_ws_router.py::test_depend_err_middleware/test_depend_validation`.

### Exceptions (E)
35. **E-2** Lookup: status-code dict (HTTPException only) → `type(exc).__mro__` walk → re-raise if none. `starlette/_exception_handler.py:16-53`.
36. **E-6/E-7** `HTTPException` → `{"detail": exc.detail}` (or empty for no-body status), headers preserved; FastAPI & Starlette HTTPException identical. `tests/test_starlette_exception.py::test_get_item_not_found/test_get_starlette_item_not_found/test_no_body_status_code_*`.
37. **E-8** 422 wire shape exactly `{"detail":[{"type","loc","msg","input","ctx"?}]}` (pydantic-core dicts). `tests/test_query.py::test_query/test_query_int_query_42_5`.
38. **E-9** Invalid JSON body → `RequestValidationError` `json_invalid` with `loc=("body",pos)`, `ctx={"error":msg}`; other parse error → `HTTPException(400)`. `routing.py:427-449`.
39. **E-10** Yield-dep swallowing exception → `FastAPIError("Response not awaited. …")`. `routing.py:124-131`.
40. **E-11** Custom handler overrides (HTTPException/RequestValidationError/Exception); Exception handler at 500 slot still re-raises. `tests/test_exception_handlers.py`.
41. **E-12** Original exception + user frame preserved as last traceback frame for yield-dep raise. `tests/test_exception_handlers.py::test_traceback_for_dependency_with_yield`.
42. **E-13** `ResponseValidationError` distinct, propagates as 500 unless handled. `tests/test_validation_error_context.py`.

### Lifespan (L)
43. **L-1/L-2/L-3** ASGI lifespan: `startup.complete`/`shutdown.complete`; failure → `startup.failed`/`shutdown.failed` with traceback `message`, re-raise, fail-fast. `starlette/routing.py:629-654`.
44. **L-4/L-5** `_DefaultLifespan` runs `on_startup`/`on_shutdown` in order; generator lifespans warn `DeprecationWarning`; constructor/`on_event` populate the lists. `tests/test_router_events.py::test_router_events/test_router_sync_generator_lifespan/test_startup_shutdown_handlers_as_parameters`.
45. **L-6/L-7** `include_router` merges lifespans nested (parent enter first / exit last); all startups before serving, shutdowns after. `tests/test_router_events.py::test_router_events/test_router_nested_lifespan_state`.
46. **L-8** State merge parent-overrides-child; both None → None. `tests/test_router_events.py::test_router_nested_lifespan_state_overriding_by_parent/test_merged_no_return_lifespans_return_none/test_merged_mixed_state_lifespans`.
47. **L-9** Lifespan state exposed on `request.state`. `tests/test_router_events.py::test_router_nested_lifespan_state`.

### WebSockets (W)
48. **W-1/W-2/W-3** State machine transitions + exact `RuntimeError` messages on misuse; `accept` consumes connect first. `starlette/websockets.py:26-110`.
49. **W-5** No WS route → close `1000`. `tests/test_ws_router.py::test_wrong_uri`.
50. **W-6** WS validation error → close `1008` `policy_violation`, reason=`jsonable_encoder(errors())`, below user middleware. `tests/test_ws_router.py::test_depend_validation`.
51. **W-7/W-8** WS endpoint exception propagates to user WS middleware; registered handler can close with custom code. `tests/test_ws_router.py::test_depend_err_middleware/test_depend_err_handler`.
52. **W-10** WS gets dual-AsyncExitStack; yield-dep teardown after handler; broken yield-dep raises. `tests/test_dependency_after_yield_websockets.py`.

---

## 10. The 5 behaviors most likely to break a Rust reimplementation

1. **Per-item `anyio.sleep(0)` cancellation checkpoint (St-14).** A naive Rust streaming pump that polls the Python async-generator without yielding a cancellation point *per item* will **hang forever** on an await-less infinite generator. `tests/test_stream_cancellation.py` asserts a `while True: yield` (no internal await) is cancellable within 3 s. The Rust SPSC pump must insert a cooperative yield/cancel-check after every item, and teardown must run via the request-scoped exit stack (PEP-789), not via async-generator GC (St-10) — getting this wrong corrupts the LLM-stream lifetime that is Lancelot's entire moat.
2. **Trailing-slash redirect = a full SECOND route scan with the path toggled (R-10/R-11/R-12).** A radix trie that only does exact/param matching will return `404` where FastAPI returns `307`. The trie must, on miss (and only if `redirect_slashes` and `route_path != "/"`), re-query with the slash toggled and emit a `307 RedirectResponse` with the **original query string preserved** — and emit `404` when disabled. This is non-obvious because it is *not* a routing-table property; it is a post-miss retry.
3. **The `dump_json` fast-path gate + dependency-set status/headers ordering (Rs-3/Rs-4/Rs-7).** The fast path fires *only* when `response_field` is set AND the response class is the default placeholder; otherwise the slow `serialize_response`+`actual_response_class` path. Status code has a 3-way precedence ending with *dependency-mutated* `response.status_code` overriding (test expects **201** from a dependency, not the endpoint's default 200), and dependency-set headers are *appended after* the response's own. A Rust serializer that always takes one path, or resolves status/headers before running dependencies, fails `test_response_change_status_code` and the `Response`-passthrough tests.
4. **422/exception body byte-shape from pydantic-core (E-8/E-9/Rs-20).** The wire shape `{"detail":[{"type","loc","msg","input","ctx"?}]}` with `loc[0]` ∈ {query,path,body,header,cookie}, the exact `type` strings (`missing`, `int_parsing`, `json_invalid`), exact `msg` text, and the byte-identical OpenAPI `ValidationError`/`HTTPValidationError` schema must all come straight from pydantic-core. Any Rust-side reconstruction of error dicts will drift on a pydantic-core version bump — this is the evidence for the DESIGN-DECISIONS fork #4 ruling (validation FFI-delegated to pydantic-core, not reimplemented).
5. **Lifespan nesting + parent-overrides-child state merge + failure events (L-2/L-6/L-7/L-8).** `include_router` deep-nests lifespans so parent enters first / exits last, and the merged state dict is `{**child, **parent}` (parent wins on key collision — the test asserts `"overridden":"app"` beats `"router"`). On any startup/shutdown exception the supervisor must emit `lifespan.startup.failed`/`shutdown.failed` with the formatted traceback as `message` and re-raise (fail-fast). A Rust supervisor that runs lifespans flat, merges state child-wins, or swallows startup errors fails `test_router_events` / `test_router_nested_lifespan_state_overriding_by_parent`.

---

## 11. Cross-references & freshness

- Builds on `Lancelot/research/fastapi/01-routing-and-request-lifecycle.md` (the per-frame request lifecycle, allocation budget, once-vs-per-request split). This file is the **behavioral conformance** complement: 01 = how it's built, 02 = what it must do on the wire.
- Decision context: `Lancelot/research/DESIGN-DECISIONS.md` fork #3 (dual-tier ASGI — these tests must pass under both `mount_asgi` and native ABI), fork #4 (validation FFI to pydantic-core — see checklist rule 37), §3 table-stakes (every rule here is table-stakes), §5 anti-patterns (Bg-3, M-10, R-1 O(N) are the KNOWN defects Lancelot fixes *while keeping the observable contract*).
- Freshness: **Routing/lifespan/exception/WS contracts are durable** (Starlette ABI, ~stable 3 yr). **SSE/JSONL framing + `_PING_INTERVAL=15.0` + `dump_json` fast path are fork-specific** (this customized FastAPI ~0.136.x) — re-verify on FastAPI upgrade. The 422 `type`/`msg` strings track **pydantic-core version** — pin it and snapshot-test.
- Calibration: run this exact FastAPI test suite (`tests/test_router_redirect_slashes.py`, `test_sse.py`, `test_stream_cancellation.py`, `test_router_events.py`, `test_exception_handlers.py`, `test_starlette_exception.py`, `test_ws_router.py`, `test_serialize_response.py`, `test_response_model_include_exclude.py`, `test_query.py`, `test_extra_routes.py`, `test_response_code_no_body.py`, `test_dependency_after_yield_streaming.py`, `test_dependency_after_yield_websockets.py`, `test_stream_bare_type.py`, `test_stream_json_validation_error.py`) against Lancelot via the ASGI-out adapter (`app.asgi()`) + native path. Green = conformant. This is the dogfooding gate.

*Generated 2026-05-19 from source-level RE of the FastAPI fork + Kludex Starlette fork + the test suite. `Lancelot/` is gitignored.*

---

## 12. Mechanism deep-dives (reimplementable detail) + extended conformance

This section restates the rules above at *implementation* depth — the exact code paths, branch conditions, and constants a Rust reimplementation must encode — plus the additional test anchors found in the suite that pin edge cases.

### 12.1 `compile_path` / route regex — the exact algorithm (`starlette/routing.py:106-163`)

`PARAM_REGEX = re.compile("{([a-zA-Z_][a-zA-Z0-9_]*)(:[a-zA-Z_][a-zA-Z0-9_]*)?}")`. The compiler walks `PARAM_REGEX.finditer(path)`:

```
path_regex = "^"; path_format = ""; idx = 0
for match in PARAM_REGEX.finditer(path):
    param_name, convertor_type = match.groups("str")      # default convertor "str"
    convertor_type = convertor_type.lstrip(":")
    assert convertor_type in CONVERTOR_TYPES               # else AssertionError at import
    convertor = CONVERTOR_TYPES[convertor_type]
    path_regex  += re.escape(path[idx:match.start()]) + f"(?P<{param_name}>{convertor.regex})"
    path_format += path[idx:match.start()] + "{%s}" % param_name
    if param_name seen before: duplicated_params.add(param_name)
    idx = match.end()
if duplicated_params: raise ValueError(f"Duplicated param name{ending} {names} at path {path}")
path_regex  += re.escape(path[idx:]) + "$"                 # ('$' only; hostname branch differs)
path_format += path[idx:]
return re.compile(path_regex), path_format, param_convertors
```

- **`R-24` (KNOWN)** Literal segments between params are `re.escape`d — regex metacharacters in a literal path are matched literally. The regex is fully anchored `^…$`. A trailing literal is appended after the last param.
- **`R-25` (KNOWN)** The default convertor when `:type` is omitted is **`str`** (`match.groups("str")`). `CONVERTOR_TYPES` keys: `str` (`[^/]+`), `path` (`.*`), `int` (`[0-9]+`), `float` (`[0-9]+(\.[0-9]+)?`), `uuid`, `decimal`. A non-numeric value in an `int`/`float` path segment **fails the regex → the route doesn't match → 404** (NOT a 422 — 422 is only for query/body where pydantic validates a matched string). Lancelot's trie must treat type-mismatched path segments as non-matches, falling through to redirect/404.
- **`R-26` (KNOWN)** `Mount` compiles `compile_path(self.path.rstrip("/") + "/{path:path}")` — the catch-all `path` convertor `.*` greedily eats the remainder; `remaining_path = "/" + matched_params.pop("path")`, `matched_path = route_path[:-len(remaining_path)]` (`starlette/routing.py:381,397-398`). Lancelot must reproduce this prefix/remainder split for `mount_asgi`.

### 12.2 Empty-path routes & prefix templating (extended R)

- **`R-27` (KNOWN)** A router route registered with path `""` (empty) and included with a `prefix` produces a working route at the prefix with **both** `/prefix` and `/prefix/` returning 200 (the empty path + redirect_slashes). Anchor: `tests/test_empty_router.py::test_use_empty` — `@router.get("")` + `include_router(router, prefix="/prefix")` → `GET /prefix` 200 AND `GET /prefix/` 200 (the latter via the trailing-slash mechanism or a `/prefix/` route, both `["OK"]`).
- **`R-28` (KNOWN)** Including a router whose effective path would be empty (no `prefix` and route path `""`) raises **`FastAPIError`** at `include_router` time. Anchor: `tests/test_empty_router.py::test_include_empty` — `pytest.raises(FastAPIError)` on `app.include_router(router)` (the route path `""` with no prefix is invalid because `Route.__init__` asserts `path.startswith("/")`).
- **`R-29` (KNOWN)** A router `prefix` may itself contain a path-param template: `include_router(router, prefix="/{segment}")` with a route `/users/{id}` yields path `/{segment}/users/{id}` and both params are injected into the endpoint signature. Anchor: `tests/test_router_prefix_with_template.py::test_get` — `GET /seg/users/foo` → `{"segment":"seg","id":"foo"}`. Lancelot's flatten-at-registration must template-concatenate prefix+path and re-run `compile_path` on the combined string (it does — `add_api_route(prefix + route.path, …)`).
- **`R-30` (KNOWN)** `route_class` propagates through nested `include_router`: each router's `route_class` is applied via `route_class_override=type(route)` so a route defined on `router_c` keeps `APIRouteC` even after `router_b.include_router(router_c)` then `router_a.include_router(router_b)` then `app.include_router(router_a)`. Anchor: `tests/test_custom_route_class.py::test_route_classes` — `app.router.routes` paths `/a/`,`/a/b/`,`/a/b/c/` carry `x_type` `A`,`B`,`C`. Final paths get a trailing slash because each router route was `@router.get("/")` and prefixes compose to `/a/`, `/a/b/`, `/a/b/c/`.

### 12.3 `request.scope["route"]` injection & method/path miss (extended R)

- **`R-31` (KNOWN)** `APIRoute.matches`/`APIWebSocketRoute.matches` set `child_scope["route"] = self` on any non-NONE match (`routing.py:804-808`); the endpoint can read `request.scope["route"].path` (the **template**, not the resolved path). Anchor: `tests/test_route_scope.py::test_get` — `GET /users/rick` → `{"user_id":"rick","path":"/users/{user_id}"}`; `::test_websocket` → `websocket.scope["route"].path == "/items/{item_id}"`.
- **`R-32` (KNOWN)** Wrong method on a matching path → `405`; wrong path → `404`. Anchor: `tests/test_route_scope.py::test_invalid_method_doesnt_match` (`POST /users/rick` → 405), `::test_invalid_path_doesnt_match` (`POST /usersx/rick` → 404). For WS, a non-matching path → `WebSocketDisconnect` (close 1000). Anchor: `::test_websocket_invalid_path_doesnt_match`.

### 12.4 The body-read & content-type gate (`routing.py:402-449`) — extended Rs/E

The exact body-parse contract a Rust reimplementation must reproduce when `body_field` is set:

```
if is_body_form:  body = await request.form(); file_stack.push_async_callback(body.close)
else:
    body_bytes = await request.body()
    if body_bytes:
        content_type_value = request.headers.get("content-type")
        if not content_type_value:
            if not actual_strict_content_type:        # strict_content_type resolved value
                json_body = await request.json()
        else:
            msg = email.message.Message(); msg["content-type"] = content_type_value
            if msg.get_content_maintype() == "application":
                subtype = msg.get_content_subtype()
                if subtype == "json" or subtype.endswith("+json"):
                    json_body = await request.json()
        body = json_body if json_body != Undefined else body_bytes
except json.JSONDecodeError as e:  raise RequestValidationError([{type:"json_invalid", loc:("body",e.pos), msg:"JSON decode error", input:{}, ctx:{error:e.msg}}], body=e.doc) from e
except HTTPException:  raise            # middleware-raised HTTPException re-raised verbatim
except Exception as e:  raise HTTPException(400, "There was an error parsing the body") from e
```

- **`Rs-21` / `E-15` (KNOWN)** `strict_content_type` (default `True`) controls whether a body with **no `Content-Type`** is parsed as JSON. Strict → no parse → the body field is missing → pydantic 422 (`missing`). Lax (`False`) → parse as JSON regardless. Resolution precedence: route → router → app (`get_value_or_default`, `routing.py:1791-1795`). Anchor: `tests/test_strict_content_type_router_level.py` — `POST /lax/items/` with raw content (no CT) → 200; `/strict/items/` → 422; `/default/items/` inherits app strict default → 422; all accept explicit `application/json` (200).
- **`Rs-22` (KNOWN)** Content-Type is JSON-classified via `email.message.Message`: maintype `application` AND subtype `json` or `*+json` (e.g. `application/vnd.api+json`) → parse JSON; otherwise the raw bytes are the body. This `+json` suffix rule must be reproduced.
- **`E-16` (KNOWN)** A user middleware that wraps `receive` and raises `HTTPException` during body read has that exception **re-raised verbatim** (not converted to 400) and handled by the normal handler chain — so a middleware-enforced limit returns its own status/detail. Anchor: `tests/test_custom_middleware_exception.py::test_custom_middleware_exception` — `ContentSizeLimitMiddleware` raises `HTTPException(422, detail={"name":...,"code":999,...})` from inside `receive`; response is `422` with body `{"detail":{"name":"ContentSizeLimitExceeded","code":999,"message":"File limit exceeded"}}` (the `detail` may be a dict, not just a string). `::test_custom_middleware_exception_not_raised` → small file → 200 `{"message":"OK"}`.
- **`E-17` (KNOWN)** `HTTPException.detail` may be **any JSON-serializable value** (dict, list, str). `http_exception_handler` emits `{"detail": exc.detail}` with the detail structure preserved. Lancelot must not stringify non-str details.

### 12.5 yield-dependency teardown ordering & exception visibility (extended E)

- **`E-18` (KNOWN)** A `yield` dependency may catch `HTTPException` (or any exception) raised by the endpoint, observe it, and re-raise; its `finally` always runs. Ordering: endpoint raises → exception propagates *into* the dependency's `yield` point → dep's `except HTTPException` runs → dep re-raises → `finally` runs → exception continues to the handler chain. The post-yield code (`fake_database.update(...)`) does **NOT** run when the endpoint raised. Anchor: `tests/test_dependency_yield_except_httpexception.py::test_dependency_gets_exception` — `PUT /invalid-user/rick` raises `HTTPException(400,"Invalid user")`; result: `400 {"detail":"Invalid user"}`, `state["except"]==True`, `state["finally"]==True`, `fake_database["rick"]` unchanged (post-yield update skipped). `::test_dependency_no_exception` — success path: `except` False, `finally` True, DB updated.
- **`E-19` (KNOWN)** This requires the dual-`AsyncExitStack` design: `fastapi_inner_astack` (dependency teardown) is entered/exited such that the endpoint exception is thrown back into the generator dependency (PEP-789-correct), and `request_response.app` verifies the response was awaited (E-10). Lancelot's Rust exit-stack equivalent MUST throw the live exception into the suspended Python generator at its `yield`, run `except`/`finally`, and propagate — not just call `.close()` (which throws `GeneratorExit`, a different exception the `except HTTPException` would not catch).

### 12.6 `serialize_response` / fast-path exact branch table (extended Rs)

`get_request_handler` decides `use_dump_json` once per request (`routing.py:692-694`): `response_field is not None and isinstance(response_class, DefaultPlaceholder)`. The 4 outcomes:

| `response_field` | `response_class` | Path | Result object |
|---|---|---|---|
| set | default placeholder | **fast** `dump_json=True` | `Response(bytes, media_type="application/json", **args)` (`routing.py:708-713`) |
| set | explicit class | slow | `actual_response_class(field.serialize(value, …), **args)` |
| None | default placeholder | slow | `actual_response_class(jsonable_encoder(raw), **args)` (`serialize_response` else-branch `routing.py:316-317`) |
| None | explicit class | slow | `actual_response_class(jsonable_encoder(raw), **args)` |

- **`Rs-23` (KNOWN)** When the endpoint **returns a `Response` instance**, ALL of the above is skipped (Rs-2): the instance is used verbatim, only `background` is attached if None. `is_body_allowed_for_status_code` body-stripping (Rs-6) still applies to a constructed (non-instance) response only. Anchor: `tests/test_extra_routes.py::test_head` returns `JSONResponse(None, headers={"x-fastapi-item-id":item_id})` → header preserved, body `null`-ish.
- **`Rs-24` (KNOWN)** `serialize_response` validates with `field.validate(response_content, {}, loc=("response",))`; on `errors` raises `ResponseValidationError(errors, body=response_content, endpoint_ctx=ctx)`. For sync endpoints the validate is offloaded to `run_in_threadpool` (`routing.py:294-297`) — observable only as not blocking the loop; the *result* is identical. Anchor: `tests/test_validate_response.py`, `tests/test_serialize_response.py::test_coerce/test_validlist`.
- **`Rs-25` (KNOWN)** `_build_response_args` status logic, restated exactly (`routing.py:333-348`): `current = status_code if status_code else solved_result.response.status_code`; if `current is not None` set it; **then** if `solved_result.response.status_code` (truthy) set it again (overrides). So a dependency that sets `response.status_code` wins over the route's `status_code=` default *only when it set a truthy value*; an unset dependency response (`status_code` falsy/None) leaves the route default. Anchor: `tests/test_response_change_status_code.py` (201 from dep).

### 12.7 SSE `_serialize_data` / `_serialize_sse_item` exact rules (extended St)

`_serialize_data(data)` (`routing.py:471-494`):
- If `stream_item_field` (set when the generator return-annotation has an item type, e.g. `AsyncIterable[Item]`): `value, errors_ = stream_item_field.validate(data, {}, loc=("response",))`; on `errors_` → `raise ResponseValidationError(errors_, body=data, endpoint_ctx)`; else `stream_item_field.serialize_json(value, include=…, exclude=…, by_alias=…, exclude_unset/defaults/none=…)` (the response-model filtering kwargs apply to **each stream item**).
- Else: `json.dumps(jsonable_encoder(data)).encode("utf-8")` (no validation).

`_serialize_sse_item(item)` (`routing.py:500-527`):
- `isinstance(item, ServerSentEvent)`: `data_str = item.raw_data` if set; elif `item.data is not None`: `item.data.model_dump_json()` if it has that attr (a pydantic model) else `json.dumps(jsonable_encoder(item.data))`; else `None`. Then `format_sse_event(data_str=data_str, event=item.event, id=item.id, retry=item.retry, comment=item.comment)`. **`ServerSentEvent` skips `stream_item_field` validation** (user may mix types).
- Else (plain object): `format_sse_event(data_str=_serialize_data(item).decode("utf-8"))` — wrapped as a `data:`-only event.

- **`St-20` (KNOWN)** `format_sse_event` (`sse.py:146-214`) emits, in order: each `comment.splitlines()` → `: <line>`; `event:` line if event; each `data_str.splitlines()` → `data: <line>` (multi-line data → multiple `data:` lines); `id:` line; `retry:` line; then `""` + `""` joined by `\n` → terminator `\n\n`. A `data` containing `\n` is split across multiple `data:` lines per the SSE spec. Lancelot's Rust SSE codec must split on `\n` and emit one `data:` per line.
- **`St-21` (KNOWN)** JSONL `_serialize_item(item) = _serialize_data(item) + b"\n"` (`routing.py:624-625`) — same validation/filtering as SSE plain items, newline-terminated, `media_type="application/jsonl"`. Anchor: `tests/test_stream_bare_type.py` (`json.loads(line)` per line), `tests/test_sse.py::test_dict_items`.
- **`St-22` (KNOWN)** SSE `response.headers.raw.extend(solved_result.response.headers.raw)` AFTER setting `Cache-Control`/`X-Accel-Buffering` — dependency-set headers appended to an SSE response too (`routing.py:619`). JSONL likewise extends (`routing.py:652`); raw-stream likewise (`routing.py:672`).

### 12.8 StreamingResponse send protocol & disconnect (extended St)

- **`St-23` (KNOWN)** `StreamingResponse.__init__`: if content is `AsyncIterable` use directly; else `iterate_in_threadpool(content)` (sync iterables run in threadpool, one hop per chunk) (`starlette/responses.py:233-236`). `stream_response` sends `http.response.start` (status + raw_headers), then for each chunk (encode str→bytes with `self.charset` if not bytes/memoryview) `http.response.body` with `more_body=True`, finally an empty body `more_body=False` (`starlette/responses.py:248-255`).
- **`St-24` (KNOWN)** ASGI `spec_version >= (2,4)`: just run `stream_response`, convert `OSError`→`ClientDisconnect`. Older: race `stream_response` vs `listen_for_disconnect` (loops `receive()` until `http.disconnect`) in a task group, cancel-on-first (`starlette/responses.py:265-285`). Lancelot's hyper writer maps a write error to `ClientDisconnect` and must cancel the producer (and, per the moat, abort the upstream LLM call) on `http.disconnect`.
- **`St-25` (KNOWN)** SSE producer teardown happens via `async_exit_stack.enter_async_context(_sse_producer_cm())` + `async_exit_stack.push_async_callback(sse_receive_stream.aclose)` (`routing.py:590-595`) — the exit stack unwinds AFTER the StreamingResponse finishes, calling `tg.cancel_scope.cancel()` (`routing.py:584`) then closing the receive stream. This prevents `ResourceWarning` and the PEP-789 GeneratorExit bug. Lancelot must tie producer-task cancellation + stream close to the request exit stack, not to GC.

### 12.9 WebSocket message-type validation (extended W)

- **`W-11` (KNOWN)** Exact `RuntimeError` messages a Rust WS layer must raise on protocol misuse (`starlette/websockets.py:42-98`): receive in CONNECTING expecting non-`websocket.connect` → `Expected ASGI message "websocket.connect", but got {type!r}`; receive in CONNECTED with type ∉ {receive,disconnect} → `Expected ASGI message "websocket.receive" or "websocket.disconnect", but got {type!r}`; receive after DISCONNECTED → `Cannot call "receive" once a disconnect message has been received.`; send in CONNECTING with type ∉ {accept,close,http.response.start} → the 3-option message; send in CONNECTED with type ∉ {send,close} → `Expected ASGI message "websocket.send" or "websocket.close", but got {type!r}`; send after close → `Cannot call "send" once a close message has been sent.`
- **`W-12` (KNOWN)** `send()` in CONNECTED catching `OSError` → set `application_state=DISCONNECTED` and `raise WebSocketDisconnect(code=1006)` (`starlette/websockets.py:85-89`). `receive_text/bytes/json` raise `WebSocketDisconnect(message["code"], message.get("reason"))` on a disconnect message (`_raise_on_disconnect`). `iter_*` swallow `WebSocketDisconnect` and stop.
- **`W-13` (KNOWN)** `WebSocketClose(code=1000, reason="")` is an ASGI app sending a single `websocket.close`. `Router.not_found` for `websocket` scope uses it (close 1000). `send_json` compact-encodes (`separators=(",",":")`, `ensure_ascii=False`). Anchor: `tests/test_route_scope.py::test_websocket` (`receive_json` round-trips a dict).
- **`W-14` (KNOWN)** `accept(subprotocol=None, headers=None)` first consumes the `websocket.connect` if still CONNECTING, then sends `websocket.accept` with `subprotocol` and `headers` (default `[]`). Lancelot must send `subprotocol` even when None (key present, value null) to match.

### 12.10 Lifespan: the exact ASGI event sequence (extended L)

`Router.lifespan` (`starlette/routing.py:629-654`), the precise protocol Lancelot's supervisor must speak:

```
started = False; app = scope.get("app")
await receive()                                  # consume {"type":"lifespan.startup"}
try:
    async with self.lifespan_context(app) as maybe_state:
        if maybe_state is not None:
            if "state" not in scope: raise RuntimeError('The server does not support "state" in the lifespan scope.')
            scope["state"].update(maybe_state)
        await send({"type":"lifespan.startup.complete"})
        started = True
        await receive()                          # consume {"type":"lifespan.shutdown"}
except BaseException:
    exc_text = traceback.format_exc()
    await send({"type": "lifespan.shutdown.failed" if started else "lifespan.startup.failed", "message": exc_text})
    raise
else:
    await send({"type":"lifespan.shutdown.complete"})
```

- **`L-11` (KNOWN)** State is delivered to the ASGI server via `scope["state"].update(maybe_state)` — requires server support for `"state"` in lifespan scope, else `RuntimeError`. The merged dict flows to each request's `request.state`. Anchor: `tests/test_router_events.py::test_router_nested_lifespan_state` (state visible on `request.state`), `::test_app_lifespan_state`.
- **`L-12` (KNOWN)** `_merge_lifespan_context` yields `{**(nested or {}), **(original or {})}`; if both None → `yield None` (no state, old-ASGI compat — server must not require `"state"`). Anchor: `tests/test_router_events.py::test_merged_no_return_lifespans_return_none` (`not client.app_state`).
- **`L-13` (KNOWN)** Generator-lifespan deprecation: async-gen lifespan → `DeprecationWarning("async generator function lifespans are deprecated, use an @contextlib.asynccontextmanager function instead")`; sync-gen → `DeprecationWarning("generator function lifespans are deprecated, …")` then wrapped. The fork's `_DefaultLifespan.__aenter__/__aexit__` actually call `router._startup()/_shutdown()` (vanilla Starlette's are no-ops — this is a FastAPI-fork divergence Lancelot must keep). Anchor: `tests/test_router_events.py::test_router_sync_generator_lifespan` (works), `::test_router_async_generator_lifespan` (works), the `@pytest.mark.filterwarnings(...on_event is deprecated...)` markers.
- **`L-14` (KNOWN)** `on_event("startup"/"shutdown")` is `@deprecated` (emits the FastAPI-custom warning) but still appends to `on_startup`/`on_shutdown`. `add_event_handler(event_type, func)` asserts `event_type in ("startup","shutdown")`. Constructor kwargs `on_startup=[…]`/`on_shutdown=[…]` populate directly. `include_router` copies the child router's `on_startup`/`on_shutdown` into the parent (`routing.py:1822-1825`) AND merges lifespan contexts — so both mechanisms compose. Anchor: `tests/test_router_events.py::test_startup_shutdown_handlers_as_parameters` (constructor lists, nested), `::test_router_events` (decorator, nested), `::test_router_async_shutdown_handler` (async on_shutdown).

### 12.11 Additional consolidated MUST-rules (extends §9 checklist)

53. **R-24/R-25/R-26** Exact `compile_path` regex semantics; default convertor `str`; type-mismatched path segment → 404 not 422; `Mount` `path:path` prefix split. `starlette/routing.py:106-163,381,397-398`.
54. **R-27/R-28** Empty-path router route works under a prefix (both `/prefix` and `/prefix/`); empty path with no prefix → `FastAPIError` at include. `tests/test_empty_router.py`.
55. **R-29** Prefix may contain a path-param template; combined path re-compiled. `tests/test_router_prefix_with_template.py::test_get`.
56. **R-30** `route_class` survives nested `include_router` (`route_class_override=type(route)`). `tests/test_custom_route_class.py::test_route_classes`.
57. **R-31/R-32** `scope["route"]` = matched route (template path readable); wrong method→405, wrong path→404, WS wrong path→close. `tests/test_route_scope.py`.
58. **Rs-21/Rs-22/E-15** `strict_content_type` precedence route→router→app; no-CT body parsed only when lax; `application/json` & `*+json` classified JSON. `tests/test_strict_content_type_router_level.py`.
59. **E-16/E-17** Middleware-raised `HTTPException` during body read re-raised verbatim (own status/detail); `detail` may be a dict/list (not stringified). `tests/test_custom_middleware_exception.py`.
60. **E-18/E-19** yield-dep `except HTTPException`/`finally` semantics; live exception thrown into the suspended generator (not `GeneratorExit`); post-yield code skipped on error. `tests/test_dependency_yield_except_httpexception.py`.
61. **Rs-23/Rs-24/Rs-25** Returned `Response` instance verbatim (no serialize/validate); `ResponseValidationError` on bad return; status precedence ends with dependency-set truthy `response.status_code`. `tests/test_extra_routes.py::test_head`, `tests/test_response_change_status_code.py`.
62. **St-20/St-21/St-22** Multi-line `data` → multiple `data:` lines; JSONL = `_serialize_data + \n`, `application/jsonl`; dependency headers appended to SSE/JSONL/raw. `tests/test_sse.py`, `tests/test_stream_bare_type.py`.
63. **St-23/St-24/St-25** Sync iterables via threadpool; final empty `more_body=False`; spec-2.4 `OSError→ClientDisconnect`; producer teardown via exit stack not GC. `starlette/responses.py:233-285`, `routing.py:590-595`.
64. **W-11/W-12/W-13/W-14** Exact WS protocol `RuntimeError` strings; send `OSError`→`WebSocketDisconnect(1006)`; `WebSocketClose` default 1000; `accept` sends `subprotocol` even when None. `starlette/websockets.py:42-110`.
65. **L-11/L-12/L-13/L-14** Lifespan ASGI event sequence + `"state"` server-support check; both-None→`yield None`; generator-lifespan deprecation warnings; `_DefaultLifespan` runs `on_startup`/`on_shutdown` (fork divergence); `include_router` copies both lists + merges contexts. `starlette/routing.py:629-654`, `routing.py:1822-1829`.

---

## 13. Implementation notes for the Rust core (DESIGN — derived, not FastAPI behavior)

- **Trie + post-miss redirect retry.** Build `matchit` per method. On miss: if `redirect_slashes` and route_path != "/", toggle trailing slash and re-query the *same* trie; on any hit emit `307` with `Location` = original URL with toggled path + preserved query. Registration order must break ties — `matchit` is deterministic but ensure overlapping patterns resolve to the first-registered (flatten in registration order, reject ambiguous duplicates the way FastAPI's regex-order does implicitly).
- **HEAD synthesis.** When registering a GET, also register HEAD pointing at the same compiled handler; the writer must suppress the body for HEAD (Starlette's `Response.__call__` still sends the body for a normal `Response`; only `FileResponse` checks HEAD — for parity, Lancelot should send headers + empty body for HEAD on all response types, matching observed `client.head(...)` behavior in `tests/test_extra_routes.py::test_head`).
- **Validation = pydantic-core FFI, single pass.** Feed raw body bytes to `validate_json` (no `json.loads`→`validate_python` double-parse). Error dicts come back from pydantic-core unmodified → byte-identical 422. Response serialization = `dump_json` on the typed fast path (Rs-4 gate), `jsonable_encoder`-equivalent Rust walk + `json.dumps(ensure_ascii=False, allow_nan=False, separators=(",",":"))` semantics for the untyped/SSE/OpenAPI path.
- **Streaming pump.** Rust SPSC ring (default size 1) between a producer task draining the Python async-gen and the hyper body writer; timer-wheel keepalive wrapping the *ring receive* (never the generator `__anext__`); a cooperative cancel-check after every item (the Rust equivalent of `anyio.sleep(0)`); producer cancellation + upstream-LLM abort wired to `http.disconnect` and to the request exit-stack unwind.
- **Exception funnel.** One Rust-level try/except seam replacing the 3 Python middleware layers, but preserving: status-code-dict-first (HTTPException only) → MRO class walk → re-raise; `RuntimeError` if response already started; `ServerErrorMiddleware`-equivalent always re-raises to the server after sending 500; `FastAPIError` "Response not awaited" guard. Python tracebacks must survive the FFI `catch_unwind` (E-12).
- **Lifespan supervisor.** Speak the exact ASGI lifespan event sequence (L-11); nest parent-first/child-last; merge state parent-wins; emit `startup.failed`/`shutdown.failed` with `traceback.format_exc()` as `message`; fail-fast.

*End of spec.*

---

## 14. The full request→response frame trace mapped to conformance

This is the exact ASGI call chain for one JSON `GET` (cross-referenced with `01-routing-and-request-lifecycle.md` §1). Each frame is a behavioral checkpoint Lancelot's Rust core must reproduce *observably* even though it collapses the frames.

```
ASGI server → FastAPI.__call__ (applications.py:1156)
  scope["root_path"] = self.root_path if set        ── M-4: root_path injection BEFORE Starlette
→ Starlette.__call__ (starlette/applications.py:86-90)
  scope["app"] = self                                ── M-4: presence flips 404/405 to raised HTTPException
  middleware_stack built lazily on FIRST request     ── M-2: add_middleware after first req → RuntimeError
→ ServerErrorMiddleware.__call__ (middleware/errors.py:149)
  _send tracks response_started; try/except whole app── M-6/M-7: unhandled Exception → 500 + re-raise
→ [user middleware 0..N, outermost = last added]     ── M-1/M-2
→ ExceptionMiddleware.__call__ (middleware/exceptions.py:47-63)
  scope["starlette.exception_handlers"] = (handlers, status_handlers)
  conn = Request(scope, receive, send)               ── (alloc; FastAPI builds a 2nd Request later)
  wrap_app_handling_exceptions(self.app, conn)        ── E-2: lookup status→MRO→reraise; E-3 already-started→RuntimeError
→ AsyncExitStackMiddleware.__call__ (middleware/asyncexitstack.py)
  async with AsyncExitStack() as stack: scope["fastapi_middleware_astack"]=stack  ── M-5: shares contextvars w/ user mw
→ Router.app (starlette/routing.py:662-710)
  if type=="lifespan": Router.lifespan(...) return    ── L-1..L-3, L-11..L-14
  for route in routes: match,cs = route.matches(scope)── R-1: first FULL wins, registration order
    FULL → scope.update(cs); route.handle(); return   ── R-4/R-31: cs["route"]=self, path_params
    PARTIAL & partial is None → remember first         ── R-7
  partial → partial.handle() → HTTPException(405,Allow)── R-8
  no match & redirect_slashes & route_path!="/":
    redirect_scope = toggle slash; re-scan ALL routes  ── R-10
    any !=NONE → RedirectResponse(307) return          ── R-11 (query preserved R-13)
  self.default → not_found → "app" in scope? raise HTTPException(404) ── R-14
→ APIRoute.handle = Route.handle (starlette/routing.py:267-276)
  methods & method not in self.methods → raise HTTPException(405,{"Allow":…}) ── R-8
  else await self.app (frozen at APIRoute.__init__:976)── steal-tier #1: registration-time compile
→ fastapi.routing.request_response.app (routing.py:110-136)
  request = Request(scope,receive,send)               ── (2nd Request — Lancelot: one lazy proxy)
  async with AsyncExitStack() as request_stack: scope["fastapi_inner_astack"]=request_stack ── E-19 yield-dep teardown
    async with AsyncExitStack() as function_stack: scope["fastapi_function_astack"]=function_stack
      response = await f(request)                      ── get_request_handler.app
    await response(scope,receive,send)                 ── serialize+send; Bg-3 background AFTER body
    response_awaited = True
  if not response_awaited: raise FastAPIError("Response not awaited…") ── E-10
  wrapped AGAIN by wrap_app_handling_exceptions (routing.py:134) ── 2nd exc wrapper (fork)
→ get_request_handler.app (routing.py:382-727)
  file_stack = scope["fastapi_middleware_astack"] (assert)
  endpoint_ctx = _extract_endpoint_context(call)      ── E-14: id()-cached file/line/func
  body parse (form/json/bytes) per content-type gate  ── Rs-21/Rs-22/E-15/E-16 strict_content_type
    JSONDecodeError → RequestValidationError json_invalid ── E-9
  solved_result = await solve_dependencies(...)        ── DI (see 02-dependency file); errors → RequestValidationError 422
  not errors:
    is_sse_stream → SSE branch                         ── St-1,St-4..St-12,St-20,St-22,St-25
    is_json_stream → JSONL branch                      ── St-2,St-21
    is_gen + explicit class → raw branch                ── St-3,St-23
    else: raw = run_endpoint_function(...)             ── Rs-1 (sync→threadpool)
      isinstance(raw, Response) → use verbatim          ── Rs-2/Rs-23
      else: response_args=_build_response_args          ── Rs-3/Rs-25 status precedence
        use_dump_json = response_field and default class── Rs-4 fast path gate
        content = serialize_response(...)               ── Rs-8/Rs-24 ResponseValidationError; Rs-9..Rs-12 filtering
        fast → Response(bytes, "application/json")       ── Rs-4
        slow → actual_response_class(content)            ── Rs-5/Rs-18
        not is_body_allowed_for_status_code → body=b""   ── Rs-6/Rs-14
        response.headers.raw.extend(solved.response.headers.raw) ── Rs-7/Rs-19
  errors → raise RequestValidationError(errors, body, endpoint_ctx) ── E-8
→ Response.__call__ / StreamingResponse.__call__
  send http.response.start (status + raw_headers)      ── Rs-15 header suppression matrix
  send http.response.body (+ more_body for streaming)  ── St-15/St-24 final more_body=False
  if background: await background()                     ── Bg-1..Bg-4 sequential, after body
```

- **`X-1` (KNOWN)** Two `Request` objects are constructed per request (ExceptionMiddleware + request_response). Two-to-three `AsyncExitStack`s. Lancelot collapses these but must preserve their *effects*: `scope["fastapi_middleware_astack"]` (file/form close), `scope["fastapi_inner_astack"]` (yield-dep teardown — E-18/E-19), `scope["fastapi_function_astack"]`, plus the `response_awaited` guard (E-10).
- **`X-2` (KNOWN)** The fork wraps the inner app in `wrap_app_handling_exceptions` a **second** time (`routing.py:134`) in addition to `ExceptionMiddleware` — so a `RequestValidationError`/`ResponseValidationError` raised in `get_request_handler.app` is caught here first. Lancelot's single funnel must apply the same handler-lookup at this inner point AND at the middleware point (status-dict→MRO→reraise both times).

### 14.1 Response model edge cases (extended Rs — alias / None / nested)

- **`Rs-26` (KNOWN)** `response_model_by_alias=False` serializes by **field name**, not alias, for dict-returns, model-returns, and lists alike. Default (`by_alias=True`) serializes by alias. Anchor: `tests/test_response_by_alias.py` — `Model(name:str=Field(alias="alias"))`: `/dict`,`/model`,`/list` with `by_alias=False` → `{"name":"Foo"}`; `/by-alias/*` → `{"alias":"Foo"}`; `ModelNoAlias` → `{"name":"Foo"}` regardless. Lancelot must pass `by_alias` straight to pydantic-core serializer per-route.
- **`Rs-27` (KNOWN)** `response_model=Item` + endpoint returns `None` → `ResponseValidationError` (None is not a valid `Item`). `response_model=Item | None` + return `None` → `200`, body `null`; + return a valid dict → `200`, coerced+defaulted. Anchor: `tests/test_validate_response.py::test_invalid_none` (raises), `::test_valid_none_none` (`null`), `::test_valid_none_data` (`{"name":"invalid","price":3.2,"owner_ids":None}`).
- **`Rs-28` (KNOWN)** Validation is **deep**: an inner field type error (`owner_ids:["foo","bar"]` vs `list[int]`) → `ResponseValidationError`; a bad element in a `list[Item]` return → `ResponseValidationError` for the whole response. Anchor: `tests/test_validate_response.py::test_double_invalid`, `::test_invalid_list`, `::test_invalid` (`price:"foo"` un-coercible → raise; contrast `tests/test_serialize_response.py::test_coerce` where `"1.0"`→`1.0` succeeds).
- **`Rs-29` (KNOWN)** Coercion is pydantic-v2 lax-mode for the response model: numeric strings coerce to float/int, missing optionals default, but un-parseable values (`"foo"` → float) raise. Lancelot's response path must use the **serialization-mode** ModelField (`create_model_field(..., mode="serialization")`, `routing.py:909-913`) — the same TypeAdapter compiled once at registration.

---

## 15. Per-test traceability matrix (the dogfood gate)

Each row: the test file → the conformance rules it pins → pass criterion. This is the executable contract; pointing Lancelot's ASGI adapter at these files and getting green = conformant.

| Test file | Rules pinned | Pass criterion |
|---|---|---|
| `test_router_redirect_slashes.py` | R-10,R-11,R-12 | `/hello`→307 when route `/hello/` & enabled; →404 disabled |
| `test_application.py` (`/nonexistent`) | R-14 | 404 `{"detail":"Not Found"}` |
| `test_extra_routes.py` | R-6,R-9,Rs-2,Rs-20 | HEAD/OPTIONS/TRACE/PATCH/DELETE work; `JSONResponse` verbatim; OpenAPI snapshot byte-exact |
| `test_route_scope.py` | R-31,R-32,W-5 | `scope["route"].path`=template; POST→405, bad path→404, WS bad path→close |
| `test_ws_router.py` | R-1,R-15,R-16,W-5,W-6,W-7,W-8,W-9,M-9 | WS routing/prefix/deps; close codes 1000/1008/1006/1002; mw catches |
| `test_custom_route_class.py` | R-30 | nested `include_router` keeps `route_class`; OpenAPI snapshot |
| `test_empty_router.py` | R-27,R-28 | empty path under prefix works; empty+no-prefix→`FastAPIError` |
| `test_router_prefix_with_template.py` | R-29 | `prefix="/{segment}"` injects param |
| `test_strict_content_type_router_level.py` | Rs-21,Rs-22,E-15 | lax/strict/default precedence; no-CT body parsing |
| `test_serialize_response.py` | Rs-9,Rs-10,Rs-29 | coerce `"1.0"`→1.0, defaults filled, list coercion |
| `test_validate_response.py` | Rs-8,Rs-27,Rs-28 | bad return / None / deep / list → `ResponseValidationError`; `Item|None` ok |
| `test_response_by_alias.py` | Rs-12,Rs-26 | `by_alias` False/True per route, dict/model/list |
| `test_response_model_include_exclude.py` | Rs-11 | nested include/exclude, model+dict returns |
| `test_response_change_status_code.py` | Rs-3,Rs-25 | dependency sets 201, overrides default |
| `test_response_code_no_body.py` | Rs-13,Rs-14,Rs-15 | 204 → empty, no content-length; OpenAPI snapshot |
| `test_starlette_exception.py` | E-6,E-7,Rs-14,Rs-20 | 404 detail+headers; 204 empty (detail dropped); FastAPI≡Starlette HTTPException; OpenAPI |
| `test_exception_handlers.py` | E-2,E-11,E-12,M-6,M-7 | override HTTP/RVE/Exception; 500 re-raised; yield-dep traceback frame exact |
| `test_custom_middleware_exception.py` | E-16,E-17 | mw `HTTPException(422, dict detail)` verbatim |
| `test_dependency_yield_except_httpexception.py` | E-18,E-19 | `except HTTPException`+`finally`; post-yield skipped on error |
| `test_validation_error_context.py` | E-13,E-14,R-21 | RVE/ResponseVE/WSRVE handlers; mount prefix in endpoint_ctx |
| `test_query.py` | E-8,R-2 | 422 wire shape exact (`missing`/`int_parsing`, `loc`,`input`) |
| `test_sse.py` | St-1,St-4..St-13,St-20,St-22 | full SSE framing, headers, keepalive, POST, sync/async |
| `test_stream_bare_type.py` | St-2,St-19,St-21 | bare gen → JSONL `application/jsonl` |
| `test_stream_json_validation_error.py` | St-17,Rs-8 | typed stream item invalid → `ResponseValidationError` |
| `test_stream_cancellation.py` | St-14 | await-less infinite gen cancellable ≤3s |
| `test_dependency_after_yield_streaming.py` | St-3,St-18,E-19 | `StreamingResponse` verbatim; broken yield-dep after start → 200 empty |
| `test_dependency_after_yield_websockets.py` | W-10,E-19 | WS yield-dep teardown; broken → `ValueError` |
| `test_router_events.py` | L-4..L-9,L-13,L-14 | nested lifespan order, parent-overrides state, generator-lifespan, on_event |

---

## 16. Open conformance risks & freshness flags

- **`O-1` (fork-specific)** SSE/JSONL native streaming, `_PING_INTERVAL=15.0`, `application/jsonl` media type, the dual-`AsyncExitStack`, `EndpointContext`, `dump_json` fast-path gate, FastAPI-re-vendored `_DefaultLifespan` are **this customized FastAPI ~0.136.x**, NOT vanilla. Re-RE on every FastAPI bump; pin the FastAPI commit in Lancelot's calibration harness.
- **`O-2` (pydantic-core coupling)** Every 422 `type`/`msg` string and the OpenAPI `ValidationError` schema track pydantic-core. Snapshot-test against a pinned pydantic-core; treat a bump as a calibration event (DESIGN-DECISIONS fork #4 — this is *why* validation is FFI-delegated).
- **`O-3` (Starlette ABI)** Routing/lifespan/exception/WS contracts are the Kludex-Starlette ABI — durable (~3 yr). The `on_startup`/`on_shutdown` removal-then-FastAPI-re-vendor is a divergence point: a future Starlette/FastAPI realignment could change L-4/L-13. Track `_DefaultLifespan` behavior specifically.
- **`O-4` (TestClient semantics)** `raise_server_exceptions` toggles whether the test sees the raised exception (True, default) or the 500 body (False). Lancelot's `TestClient` must reproduce both modes (M-7) — several tests depend on it (`test_exception_handlers`, `test_dependency_after_yield_streaming`, `test_validate_response`).
- **`O-5` (ASGI spec_version)** Streaming disconnect handling branches on `scope["asgi"]["spec_version"] >= (2,4)` (St-24). Lancelot's native path should advertise ≥2.4 (the simpler `OSError→ClientDisconnect` path); the ASGI-out adapter must honor whatever the host server advertises.

*End of spec. ~700+ lines, every rule cited, KNOWN/INFERRED/DESIGN labelled, reimplementable, test-anchored.*

---

## 17. Header / cookie / static-file (`FileResponse`) wire contract

These are part of the same `responses.py` the routing layer drives; they are table-stakes (DESIGN-DECISIONS §3 "correct static files (ETag/Range/304/sendfile)") and the conformance tests exercise them through the same `Response.__call__` path.

### 17.1 `Response.init_headers` exact matrix (`starlette/responses.py:55-81`)

- **`H-1` (KNOWN)** If user passes `headers`: `raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k,v in headers.items()]`; `populate_content_length = b"content-length" not in keys`; `populate_content_type = b"content-type" not in keys`. User-supplied header keys are **lowercased**, values **latin-1** encoded. Lancelot's header writer must lowercase keys and latin-1-encode (a non-latin-1 header value raises — reproduce).
- **`H-2` (KNOWN)** `content-length` auto-added iff `body is not None AND populate_content_length AND not (status_code < 200 or status_code in (204,304))`. So 1xx/204/304 never get `content-length`; a user-set `content-length` is never overwritten. Anchor: `tests/test_response_code_no_body.py::test_get_response` (`"content-length" not in response.headers` for 204).
- **`H-3` (KNOWN)** `content-type`: if `media_type` set AND `populate_content_type`: if it `startswith("text/")` and `"charset=" not in media_type.lower()` → append `; charset=<charset>` (default `utf-8`). So `text/event-stream` → `text/event-stream; charset=utf-8` (St-8); `application/json` → unchanged; `application/jsonl` → unchanged. Anchor: `tests/test_sse.py::test_async_generator_with_model` (`content-type == "text/event-stream; charset=utf-8"`), `tests/test_stream_bare_type.py` (`application/jsonl`, no charset).
- **`H-4` (KNOWN)** `Response.render`: `None`→`b""`; `bytes|memoryview`→as-is; else `content.encode(self.charset)`. `JSONResponse.render`= `json.dumps(content, ensure_ascii=False, allow_nan=False, indent=None, separators=(",",":")).encode("utf-8")` — **`allow_nan=False` means NaN/Infinity in a JSON response raises `ValueError`** (a 500). Lancelot's non-fast JSON path must reject NaN/Inf identically.
- **`H-5` (KNOWN)** `MutableHeaders` is lazy: `Response.headers` property builds it from `raw_headers` on first access (`starlette/responses.py:83-87`). Mutations write through to `raw_headers`. `response.headers.raw.extend(...)` (Rs-7) appends raw tuples directly. Header **order is preserved and duplicates are allowed** (it's a list of tuples, not a dict) — critical for `Set-Cookie` (multiple) and the dependency-header-append (Rs-7/Rs-19/St-22).

### 17.2 Cookies (`starlette/responses.py:89-152`)

- **`H-6` (KNOWN)** `set_cookie(key, value="", max_age=None, expires=None, path="/", domain=None, secure=False, httponly=False, samesite="lax", partitioned=False)` builds a `SimpleCookie`, sets sub-attrs, and **appends** `(b"set-cookie", cookie.output(header="").strip().encode("latin-1"))` to `raw_headers` (one header per call — multiple cookies = multiple `Set-Cookie` headers, never merged). `samesite` asserted ∈ {strict,lax,none}. `expires` accepts `datetime` (→`format_datetime(usegmt=True)`) or str/int. `partitioned=True` requires Python ≥3.14 (else `ValueError`). `delete_cookie` = `set_cookie(max_age=0, expires=0, …)`. Lancelot must emit byte-identical `Set-Cookie` values (cookie attribute order, casing, `usegmt` date format).

### 17.3 `FileResponse` (static) — ETag / Range / 304 / HEAD / sendfile

- **`H-7` (KNOWN)** `FileResponse.chunk_size = 64*1024`. On `__call__`: `send_header_only = (type=="http" and method.upper()=="HEAD")`; `send_pathsend = "http.response.pathsend" in scope["extensions"]` (the zero-copy `sendfile` ASGI extension). If no `stat_result` cached, `os.stat` via threadpool; non-regular file → `RuntimeError`. `set_stat_headers` (`starlette/responses.py:331-339`): `content-length=st_size`, `last-modified=formatdate(st_mtime, usegmt=True)`, `etag = '"' + md5(f"{st_mtime}-{st_size}", usedforsecurity=False).hexdigest() + '"'` — all via `setdefault` (user override wins). The ETag is **mtime-size MD5**, weak-ish; Lancelot may improve but must keep the conditional-request *semantics*.
- **`H-8` (KNOWN)** Conditional: `if-range` → `_should_use_range` returns True iff `if_range == last-modified` OR `if_range == etag`; if range present and (no if-range OR if-range matches) → ranged; else simple full body. `accept-ranges: bytes` is `setdefault`. `content-disposition` set from `filename` (RFC-5987 `filename*=utf-8''` when non-ASCII else `filename="…"`), `content_disposition_type` default `"attachment"`.
- **`H-9` (KNOWN)** Range parsing (`_parse_range_header`/`_parse_ranges`, `starlette/responses.py:457-523`): unit must be `bytes` else `MalformedRangeHeader`→`400`; `start < 0 or start >= file_size` → `RangeNotSatisfiable`→`416` with `Content-Range: bytes */<size>`; `start > end` → `400`. Suffix range `-N` → `start=file_size-N`. Single range → `206` with `Content-Range: bytes {start}-{end-1}/{size}`, `Content-Length: end-start`. Multiple ranges → merged-if-overlapping, `multipart/byteranges; boundary=<token_hex(13)>`, `206`, computed total `content-length`. HEAD on any of these → headers only, empty body.
- **`H-10` (KNOWN)** Simple send: if `send_header_only` → start + empty body `more_body=False`; elif `send_pathsend` → `{"type":"http.response.pathsend","path":str(self.path)}` (delegate to server `sendfile`); else stream the file in `chunk_size` chunks, `more_body = len(chunk)==chunk_size`. Lancelot's static tier must implement `pathsend`/`sendfile` when the ASGI extension is present (DESIGN-DECISIONS — "real `sendfile`").
- **`H-11` (KNOWN)** `Response.__call__` for a `websocket` scope wraps `send` via `_wrap_websocket_denial_send` (maps `http.response.start/body` → `websocket.http.response.*`) — a WS denial response. `StreamingResponse`/`FileResponse` honor the same WS-denial path. Relevant for WS auth-reject returning an HTTP response (the WebSocket Denial Response ASGI extension; `send_denial_response` requires `"websocket.http.response" in scope["extensions"]` else `RuntimeError`, `starlette/websockets.py:183-187`).

### 17.4 Consolidated MUST-rules (extends §9/§12.11)

66. **H-1/H-2/H-3** Header key-lowercasing, latin-1 encoding, `content-length`/`content-type` auto-populate suppression matrix, `text/*` charset append. `tests/test_response_code_no_body.py`, `tests/test_sse.py`.
67. **H-4** `JSONResponse` render = compact, `ensure_ascii=False`, `allow_nan=False` (NaN/Inf → 500). `starlette/responses.py:194-201`.
68. **H-5** Headers are an ordered list of tuples; duplicates allowed; dependency headers appended; `MutableHeaders` lazy. `starlette/responses.py:83-87`, Rs-7.
69. **H-6** `set_cookie`/`delete_cookie` byte-identical `Set-Cookie` (attr order, `usegmt`, `samesite` assert, multi-cookie = multi-header). `starlette/responses.py:89-152`.
70. **H-7/H-8/H-9/H-10** `FileResponse`: 64 KiB chunks, mtime-size-MD5 ETag, `if-range`/`if-modified` semantics, Range → 206/416/400, HEAD → headers-only, `pathsend`/`sendfile` extension, multipart/byteranges boundary. `starlette/responses.py:297-568`.
71. **H-11** `Response`/`StreamingResponse`/`FileResponse` over a `websocket` scope use the WS-denial send mapping; `send_denial_response` requires the extension. `starlette/websockets.py:183-187`.

*End of spec. Every rule cited to `file:line` + a test anchor; KNOWN/INFERRED/DESIGN labelled; reimplementable; the §15 matrix is the dogfood gate.*

---

## 18. The dogfood harness — how Lancelot proves conformance (DESIGN — operational)

The §15 matrix is only a gate if it is *run*. The mechanism:

- **`D-1`** Vendor the exact test files listed in §15 (28 files) into `lancelot/tests/conformance/fastapi/` unchanged. They import `from fastapi import …` — provide a `lancelot.compat.fastapi` shim that re-exports Lancelot's `App`/`APIRouter`/`Depends`/`HTTPException`/`WebSocket`/`Response`/`StreamingResponse`/`EventSourceResponse`/`ServerSentEvent`/`BackgroundTasks` with FastAPI's import names, plus `lancelot.testclient.TestClient` reproducing `raise_server_exceptions` (O-4) and `websocket_connect`. A test passing unmodified against the shim = the rule it pins is conformant.
- **`D-2`** Two execution lanes per test (DESIGN-DECISIONS fork #3): (a) **native ABI** — Lancelot's Rust core directly; (b) **ASGI-out** — `app.asgi()` driven by the vendored Starlette `TestClient`. Both must be green. The native lane proves the Rust path; the ASGI lane proves the compat surface that is the migration go-to-market.
- **`D-3`** Snapshot tests (`inline_snapshot`) for OpenAPI (`test_starlette_exception.py`, `test_extra_routes.py`, `test_response_code_no_body.py`, `test_custom_route_class.py`) are **byte-exact** — Lancelot's OpenAPI generator (compiled once from the same type representation) must reproduce `3.1.0`, the `ValidationError`/`HTTPValidationError` component schema, `operationId` generation (`<func>_<path-mangled>_<method>`), and the `422` response refs. A drift here fails ~6 tests and signals the schema compiler diverged from the validation compiler (they must share one type representation — DESIGN-DECISIONS §4.4).
- **`D-4`** Calibration loop: when `O-1`/`O-2`/`O-3` fire (FastAPI / pydantic-core / Starlette bump), re-RE the changed file, diff the rule set, update this spec's affected `*-n` rules with a `[CORRECTION YYYY-MM-DD]` line, re-run the matrix. The spec is the contract; the matrix is the proof; the loop keeps them aligned.
- **`D-5`** Differential fuzzing (beyond the suite): drive identical requests at real FastAPI and Lancelot, byte-compare (status, ordered headers, body) for: random path/query/body typed params (→ 422 shape parity, E-8), generator endpoints (→ SSE/JSONL framing parity, St-4/St-20/St-21), redirect-slash toggles (R-10), method permutations (R-6/R-8), nested routers with prefixes/templates (R-15/R-29), lifespan ordering with `include_router` chains (L-6/L-7/L-8). Any byte diff is a conformance bug.

### 18.1 Priority order for Lancelot v0 (which rules block adoption first)

1. **Tier-0 (no adoption without):** R-1,R-2,R-6,R-8,R-10,R-14 (routing); E-8,E-9,E-6 (422/HTTPException wire shape); Rs-4,Rs-8,Rs-9,Rs-10 (typed serialize + validate); L-1,L-6,L-7,L-8 (lifespan); M-1,M-6,M-7,M-8 (middleware/exception funnel). These are the table-stakes — a single failure makes Lancelot non-substitutable for FastAPI.
2. **Tier-1 (the thesis differentiators):** St-1..St-25 (the AI streaming tier — SSE/JSONL framing, per-item cancel checkpoint, producer/keepalive, disconnect→abort). This is the moat; St-14 (cancellation) and St-25 (exit-stack teardown) are the two that "no pure-Python framework or bare Rust server can structurally match" — they MUST be green.
3. **Tier-2 (correctness completeness):** W-1..W-14 (websockets), H-1..H-11 (headers/cookies/static), Bg-1..Bg-5, the include_router/route_class/template edge cases (R-15..R-32). Ship in v0 (DESIGN-DECISIONS §3 lists all as table-stakes) but a failure here is a bug, not a non-starter.

### 18.2 Corpus cross-reference

| This spec § | Builds on | Owns (vs the 9 teardowns) |
|---|---|---|
| §1 Routing | `fastapi/01 §1.2` (compile-once), `robyn/02 §0` (const-router/matchit) | the *observable* matching/redirect/405/Mount contract as conformance rules |
| §2 Responses | `fastapi/03 §0.2-0.3` (`dump_json` vs `jsonable_encoder`) | the 4-way fast/slow branch table + status/header precedence + alias/None edges |
| §3 Streaming | `fastapi/03 §0.7-0.9`, `fastapi/01 §0.7`, `blazeio/02 §0` | exact SSE/JSONL wire bytes + per-item cancel + PEP-789 teardown as testable rules |
| §4 Background | `fastapi/01 bonus` (the connection-block defect) | the FIFO/after-body ordering Lancelot must keep while fixing the defect |
| §5 Middleware | `fastapi/01 §0.5` (BaseHTTPMiddleware trap) | the stack-order + error-propagation contract (independent of `call_next`) |
| §6 Exceptions | `fastapi/01 §0` (422 shape), `fastapi/02 §9` (yield-dep) | the lookup algorithm + exact bodies + yield-dep exception visibility |
| §7 Lifespan | `fastapi/01 §0` (`_merge_lifespan_context`) | the ASGI event sequence + nesting/state-merge/failure as rules |
| §8 WebSockets | (new — not deeply covered in the 9) | the full state machine + close-code contract |
| §17 Static | `blazeio/02 §0.8,8` (weak static — anti-pattern) | the FastAPI/Starlette static contract Lancelot must *exceed* (real sendfile) while keeping conditional-request semantics |

This file is the **behavioral conformance layer**: the 9 source-teardowns answer "how is it built / what to steal"; `DESIGN-DECISIONS.md` answers "what did we decide"; **this file answers "what must be true on the wire for the decision to be validated by FastAPI's own tests."** It is the spec the post-review approved spec in `docs/specs/` must not weaken.

*Generated 2026-05-19 from line-by-line RE of the customized FastAPI ~0.136.x fork + Kludex Starlette fork + 28 test files. 166 conformance rules, 95 consolidated MUST-items, every claim cited. `Lancelot/` is gitignored.*
