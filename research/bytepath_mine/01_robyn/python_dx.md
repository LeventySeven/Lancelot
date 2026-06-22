# Robyn DX layer — mechanism-level teardown
## Source cluster: `dependency_injection.py`, `authentication.py`, `openapi.py`, `ai.py`, `mcp.py`, `pydantic_support.py`, `responses.py`, `_param_utils.py`, `ws.py`
## Status vs Lancelot: additive (M1–M19 shipped); every item tagged HAVE / NEW / IMPROVE / SKIP

---

## 1. `responses.py` — SSE framing helper: `SSEMessage()`

**Mechanism:** `responses.py:167–206` — `SSEMessage(data, event=None, id=None, retry=None) -> str`.  
Fast path: `"\n" not in data_str and "\r" not in data_str` → single `f"data: {data_str}\n"` (no branch into multi-line).  
Multi-line: `replace("\r\n","\n").replace("\r","\n")` then per-line loop.  
Final: `"".join(parts)` (single allocation).  
Also: `SSEResponse(content, status_code, headers) -> StreamingResponse` (shorthand constructor with `media_type="text/event-stream"`).

**Lancelot state:** Lancelot has `Event.render()` (`stream.py:32–40`) — typed event with `name`/`data`/`id` fields, JSON-serializes `data` via `_json.dumps`. It does NOT have:
- a `retry:` field on `Event` (SSE reconnect hint — useful for AI streaming where you want the client to reconnect quickly after a network hiccup, not the browser's default 3s).
- a standalone `SSEMessage(data, event=None, id=None, retry=None)` string-formatter for users who return raw bytes or pre-formatted strings instead of using `Event`.

**Byte-path relevance:** The `retry:` field controls how quickly a disconnected client reconnects — set low (e.g. 500ms) for AI streams so the user's agent resumes quickly. This is an operator-experience gap, not a throughput win. Low-build cost (one field on `Event.render()`).

**Verdict: IMPROVE** — add `retry: int | None` to `Event` dataclass and render it. The `SSEMessage` raw-string builder is SKIP (Lancelot's typed `Event` is better DX).

---

## 2. `responses.py` — `AsyncGeneratorWrapper` (sync-bridging async generators)

**Mechanism:** `responses.py:65–121` — wraps an async generator into a sync iterator by calling `loop.run_until_complete(get_next())` per chunk. One `run_until_complete` crossing per chunk. Robyn itself documents it as suboptimal: "The Rust implementation detects async generators but falls back to Python wrapper."

**Lancelot state:** Lancelot's R3 pump (`src/stream.rs`) is the correct architecture: ONE bridge crossing per stream, `_drain` coroutine runs ON the asyncio loop, `async for` drives the gen natively, bounded-mpsc to hyper. Robyn's `AsyncGeneratorWrapper` is the OLD broken approach Lancelot explicitly fixed (CLAUDE.md Tier-D: "do not regress R3").

**Verdict: HAVE + SKIP.** Lancelot solved this correctly; Robyn's wrapper is the anti-pattern Lancelot fixed.

---

## 3. `_param_utils.py` — type-safe individual query-param resolver with list coercion

**Mechanism:** `_param_utils.py:93–167` — `resolve_individual_params(unresolved_params, query_params, path_params, route_param_names)`.  
- `unwrap_optional(annotation)` handles both `typing.Union` and PEP 604 `X | Y` (`types.UnionType`) → `(inner_type, is_optional)`.  
- `is_list_type(annotation)` + `get_list_element_type(annotation)` → `list[T]` detection, calls `query_params.get_all(param_name)` for multi-value params.  
- `coerce_value(value, target_type, param_name)`: `str/int/float/bool` fast paths + bool string table (`frozenset({"true","1","yes","on"})` / `frozenset({"false","0","no","off",""})`), then fallback `target_type(value)` for Enum/UUID.  
- `parse_route_param_names(endpoint)` — character-walk of `:param` syntax.  
- `QueryParamValidationError` — carries `param_name`, `value`, `expected_type`, `detail`.

**Lancelot state:** Lancelot's DI (`di.py:331`) handles `list[int] = Query()` for multi-value query params and has Optional unwrapping. It goes deeper (alias, validation_alias, constraints, pydantic TypeAdapter) but is route-registration-time — it builds `FieldInfo` plans.

The SPECIFIC gap is the `frozenset` bool string table (`"yes"/"on"/"no"/"off"`) — Lancelot's bool coercion (via pydantic's `bool` coercer) handles `"true"/"1"/"false"/"0"` but may not handle `"yes"/"on"/"no"/"off"` (pydantic v2 `strict=False` may or may not — worth verifying). Robyn's `_BOOL_TRUE_STRINGS` / `_BOOL_FALSE_STRINGS` are explicit.

**Byte-path relevance:** Non-bottleneck (param parsing is not the hot path). The bool coercion coverage is DX-correctness, not perf.

**Verdict: CHECK** — verify pydantic v2 lax-mode bool coercion covers "yes"/"on". If yes: HAVE. If no: minor add to `di.py` coerce path.

---

## 4. `pydantic_support.py` — `model_validate_json` single-pass + `orjson` list serialization

**Mechanism:** `pydantic_support.py:116–137` — `validate_pydantic_body(model_class, body)` calls `model_class.model_validate_json(body)` (accepts str/bytes/bytearray natively, single-pass parse+validate). Error path calls `_sanitize_errors(e.errors())` which makes pydantic v2 error dicts JSON-serializable (bytes→str, tuple→list for `loc`, dict ctx → string values).

`serialize_pydantic_response` (`pydantic_support.py:160–176`): single model → `res.model_dump_json()`; list of models → `orjson.dumps([item.model_dump(mode="python") for item in res]).decode("utf-8")` (note: imports `orjson` at module scope — line 14 — breaking the lazy-import rule).

**Lancelot state:** Lancelot's `routing.py` detects `model_validate_json` at registration and uses it on the hot path (single-pass parse+validate). This is HAVE. The `_sanitize_errors` pattern (making pydantic v2 error dicts JSON-serializable with bytes-in-input, tuple-loc, dict-ctx) may be partially present — worth checking.

**Critical gap in Robyn:** `import orjson` at module scope (`pydantic_support.py:14`). This imports orjson even when not used. Robyn's cardinal rule violation; Lancelot's `_wire_json` lazy-imports orjson correctly (`validation.py:283–290`). **Not a Lancelot gap — this is a Robyn anti-pattern Lancelot already avoids.**

**Verdict: HAVE** (pydantic-core single-pass validate) + Robyn's orjson eager-import is an anti-pattern Lancelot correctly avoids.

---

## 5. `mcp.py` — MCP resources + prompts primitives beyond tools

**Mechanism:** `mcp.py:125–461` — `MCPHandler` stores separate registries for resources (`uri→handler`), tools (`name→handler`), and prompts (`name→handler`). Decorators:
- `@app.mcp.resource(uri="echo://{message}", name=..., mime_type=...)` — URI template matching via regex (`_match_uri_template` at `mcp.py:202–232`), path-vs-single-segment distinction for `path`/`file_path`/`directory` params (`(.+)` vs `([^/]+)`).
- `@app.mcp.prompt(name=..., description=..., arguments=[...])` — generates prompt argument schemas from function signature (`_generate_prompt_args_from_function`).
- MCP `initialize` response (`mcp.py:187–193`): `protocolVersion: "2024-11-05"`, capabilities include `resources.subscribe: False / listChanged: False`.

**Lancelot state:** Lancelot's `mcp.py` only implements `tools/list` + `tools/call` from the three MCP object types. It does NOT implement:
- `resources/list` + `resources/read` (the MCP Resources primitive).
- `prompts/list` + `prompts/get` (the MCP Prompts primitive).
- URI template matching for resources.

**Byte-path relevance:** MCP Resources (especially with URI templates like `db://{table}/{id}` or `file://{path}`) are THE mechanism Claude Desktop uses to give agents read access to context. Not having them means a Lancelot-served MCP server can't expose dynamic context to an agent. This IS an operator-experience gap for AI-serving use cases.

**Verdict: NEW (medium priority)** — add `resources/list` + `resources/read` with URI template matching to `lancelot/mcp.py`. The prompts primitive is lower priority (tools dominate actual usage). Robyn's implementation gives a complete reference. Note: Robyn's `protocolVersion: "2024-11-05"` is stale vs Lancelot's `"2025-06-18"` — Lancelot is ahead on spec compliance here.

---

## 6. `ws.py` — channel-backed WebSocket with `broadcast` + `on_connect`/`on_close` decorators

**Mechanism:** `ws.py:24–92` — `WebSocketAdapter` wraps a `WebSocketConnector` + a Rust `message_channel`. Key methods:
- `receive_json()`: `orjson.loads(text)` (ws.py:52).
- `send_json(data)`: `orjson.dumps(data).decode()` (ws.py:66).
- **`broadcast(data)`** (`ws.py:69–71`): `await self._connector.async_broadcast(data)` — sends to ALL connected clients on the endpoint. One call from Rust-backed channel.
- `id` property: connection UUID from Rust (`ws.py:79`).
- `query_params` property: access connection URL query params (`ws.py:83`).

`create_websocket_decorator` (`ws.py:92–322`): modern WS API with `on_connect`/`on_close` sub-decorators. The handler runs as an `asyncio.Task` (`ws.py:202`). Close handler waits with `asyncio.wait_for(task, timeout=5.0)` then cancels (`ws.py:237–247`).

**Lancelot state:** Lancelot's `WebSocket` (`websockets.py`) is a pure-ASGI Starlette-shape wrapper (accept/receive_*/send_*/close). It does NOT have:
- `broadcast()` — there is NO mechanism to send to all connected WS clients from a handler.
- `ws.id` — no connection identity.
- `on_connect`/`on_close` sub-decorators.
- `query_params` on the WS object (params must be extracted via standard route handling).

Lancelot's WS uses stdlib `json.dumps`; Robyn uses `orjson.loads`/`orjson.dumps`.

**Byte-path relevance:** `broadcast` is meaningful for AI-serving (agent status updates pushed to multiple browser clients simultaneously — e.g. a running agent emits progress to every open tab). Without it, users must maintain their own `set[WebSocket]` and loop. NOT a throughput bottleneck (WS is not the AI-serving hot path) but is a meaningful operator-experience gap.

`send_json`/`receive_json` with orjson is a micro-improvement (speed irrelevant at WS rates). WS JSON uses are typically signaling-scale, not throughput-scale.

**Verdict:** `broadcast` → NEW (low-medium priority, DX win for multi-client AI status push). `on_connect`/`on_close` decorators → SKIP (ASGI seam lifespan covers connect/disconnect cleanly; no product citations for this pattern). `ws.id` → SKIP (not needed if broadcast is not exposed). `ws.query_params` → HAVE (standard route params work).

---

## 7. `authentication.py` — abstract `AuthenticationHandler` + `TokenGetter` + `BearerGetter`

**Mechanism:** `authentication.py:16–96` — two ABCs: `TokenGetter` (get/set token from request, `scheme` property) + `AuthenticationHandler` (holds a `TokenGetter`, emits `unauthorized_response` with `WWW-Authenticate: <scheme>`, abstract `authenticate(request) -> Identity | None`). `BearerGetter` (`authentication.py:75–96`): checks `Authorization` header, returns `token[7:]` after "Bearer ".

**Lancelot state:** Lancelot has `StaticTokenGate` (`auth.py`) — a complete ASGI middleware for constant-time static-token gating. It also has `security.py` (FastAPI-compatible `HTTPBearer`, `OAuth2PasswordBearer`, `APIKeyHeader` — credential extractors). `StaticTokenGate` covers the guard side; `security.py` covers the extraction side.

**Verdict: HAVE.** Robyn's auth is simpler (no constant-time compare, no scope enforcement). Lancelot's implementation is a strict superset on the things that matter for the byte path (constant-time hash compare, host-spoof safety).

---

## 8. `dependency_injection.py` — global vs router-scoped DI with `merge_dependencies`

**Mechanism:** `dependency_injection.py:1–71` — `DependencyMap`: `global_dependency_map` (flat dict), `router_dependency_map` (keyed by router object). `merge_dependencies(target_router)` copies non-colliding global deps into another router's map (used when subrouters are mounted).

**Lancelot state:** Lancelot has FastAPI-style `Depends()`/`Security()` DI with scope=function/request, app-level `FastAPI(dependencies=[...])`, `include_router`. This is a strict superset (yield deps, scoped teardown, async, SecurityScopes chain, registered at route-plan-build time). Robyn's DI is a plain dict lookup — it does NOT have yield deps, async deps, or scoped teardown.

**Verdict: HAVE** (strongly — Lancelot's DI is far richer).

---

## 9. `openapi.py` — schema collision detection + subrouter merge

**Mechanism:** `openapi.py:236–268` — `_merge_component_schemas(incoming)`: iterates incoming schemas, logs a WARNING when a name already exists with a different shape ("the later definition will be used"). `add_subrouter_paths` pulls component schemas from subrouter's spec and merges them. Also: `override_openapi(path)` (`openapi.py:474–481`) — load a pre-made `openapi.json` file and bypass the generation logic entirely.

**Lancelot state:** Lancelot generates OpenAPI via `openapi.py` (pydantic `GenerateJsonSchema`, `operation_id`, `parameters_for`, etc.). The schema-collision-detection warning (`_merge_component_schemas`) and the **`override_openapi`-style static-file override** are worth checking.

**Byte-path relevance:** Neither is a byte-path win. `override_openapi` is an operator-experience feature (teams that maintain a hand-crafted spec and don't want generation to touch it). Schema collision detection is a developer warning.

**Verdict: SKIP** (not byte-path). The `override_openapi` pattern is operator DX but not on the mission-critical list.

---

## 10. `ai.py` — in-process `Agent` + `Memory` wrappers (AI logic layer)

**Mechanism:** `ai.py` — full POC AI module: `AIConfig` (reads from env), `MemoryProvider` ABC + `InMemoryProvider`, `Memory` (add/get/clear per user_id), `AgentRunner` ABC + `SimpleRunner` (lazy-imports `openai`, calls `client.chat.completions.create`, injects last 10 history messages). `Agent.run(query, history=)` loads memory, calls runner, stores interaction.

**Lancelot state (+ cardinal rule):** This is exactly the AI-logic-user-side rule from CLAUDE.md: "AI logic stays on the user side." `ai.py` wraps OpenAI's chat completion, maintains in-process session memory, and orchestrates agent loops — ALL of which are user-side concerns. Lancelot owns the TRANSPORT (TransportPool, fanout, streaming pump, TTFT); the agent loop is the user's job.

**Verdict: SKIP (categorical).** This is the Tier-D AI-logic boundary. Robyn crossed it (experimental, per the module docstring). Lancelot must not.

---

## Summary table

| Source | Mechanism | Tag | Priority |
|---|---|---|---|
| `responses.py:167` | `SSEMessage(retry=)` / `Event.retry` field | IMPROVE | Low |
| `responses.py:65` | `AsyncGeneratorWrapper` (per-chunk bridge) | HAVE+SKIP | — |
| `_param_utils.py:93` | `resolve_individual_params` + bool `yes/on` coercion | CHECK | Low |
| `pydantic_support.py:116` | `model_validate_json` single-pass | HAVE | — |
| `pydantic_support.py:14` | `import orjson` at module scope | Robyn anti-pattern | — |
| `mcp.py:125` | MCP Resources + Prompts (resources/list, resources/read, URI templates) | NEW | Medium |
| `ws.py:69` | WS `broadcast()` to all connected clients | NEW | Low-Medium |
| `ws.py:302` | `on_connect`/`on_close` sub-decorators | SKIP | — |
| `authentication.py:48` | `AuthenticationHandler` + `BearerGetter` ABCs | HAVE | — |
| `dependency_injection.py:52` | `merge_dependencies` | HAVE (superset) | — |
| `openapi.py:236` | Schema collision warning + subrouter merge | SKIP | — |
| `openapi.py:474` | `override_openapi` static file bypass | SKIP | — |
| `ai.py` | Agent/Memory/Runner (in-process OpenAI wrapper) | SKIP (Tier-D) | — |
