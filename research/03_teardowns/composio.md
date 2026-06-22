# Composio — Mechanism-Level Teardown (Lancelot harvest)

**Sources:** `COMPOSIO_DEEP.md`, `COMPOSIO_R4_CORE.md`, `COMPOSIO_R4_TOOLS.md`, `COMPOSIO_R4_ROUTER.md`  
**Versions read:** Python `composio 1.0.0-rc2` · TS `@composio/core 0.10.0` · Stainless client `@composio/client 0.1.0-alpha.73`  
**Focus per brief:** meta-tools (6/9 the LLM sees, 10K+ server-side), COMPOSIO_MULTI_EXECUTE, 3-point modifier engine, execution envelope `{data,error,successful}`, deferred tool search.

---

## 1. The core architectural bet

Composio v0.12 exposes 6 meta-tools to the LLM. The agent NEVER sees the 10K+ app tools directly. This collapses prompt-context from ~50K tokens to ~6 schemas. The 6 meta-tools are:

| slug | role | server cap |
|---|---|---|
| `COMPOSIO_SEARCH_TOOLS` | planner entry point; returns tools + plan + memory | batched N parallel sub-queries |
| `COMPOSIO_GET_TOOL_SCHEMAS` | lazy schema hydration for slugs from SEARCH | up to ~6 per query |
| `COMPOSIO_MANAGE_CONNECTIONS` | OAuth/API-key connect; returns branded auth link | — |
| `COMPOSIO_MULTI_EXECUTE_TOOL` | parallel batch executor | **50 tools per call** |
| `COMPOSIO_REMOTE_WORKBENCH` | persistent Jupyter kernel, 180s cell limit | 4 tiers: 1vCPU/1GB → 8vCPU/8GB |
| `COMPOSIO_REMOTE_BASH_TOOL` | bash in same sandbox | 180s limit |

Three more exist in the `execute_meta` enum (callable but not listed by `/tools`): `COMPOSIO_WAIT_FOR_CONNECTIONS`, `COMPOSIO_UPSERT_RECIPE`, `COMPOSIO_GET_RECIPE`.

**Source:** `COMPOSIO_R4_ROUTER.md` R4.1 (Stainless `session.d.ts:2974` + `openapi-v3.json`); the 6-vs-9 discrepancy is confirmed as a listing/rendering decision, not a config gate (`R4_ROUTER.md` R5.0 `[CORRECTION]`).

---

## 2. COMPOSIO_MULTI_EXECUTE_TOOL — the exact mechanism

```python
# tool_router_session.py:70
MAX_PARALLEL_WORKERS = 5   # CLIENT-SIDE ThreadPoolExecutor — NOT the server cap
```

**Server cap: 50** (`meta-tools.json` `maxItems:50`). Two different numbers at two layers. The confusion in prior teardowns was treating the client's `MAX_PARALLEL_WORKERS=5` as the batch cap.

**What it does:**
1. LLM emits ONE `COMPOSIO_MULTI_EXECUTE_TOOL` call with an array of ≤50 `{tool_slug, arguments}` items.
2. SDK partitions into **local** (custom tools, `find_custom_tool`) vs **remote** (app tools) items (`tool_router_session.py:282`).
3. Remote items are sent in **ONE single backend call** (the whole batch, `POST /api/v3.1/tool_router/session/{id}/execute_meta`). Not N calls.
4. Local items run via `ThreadPoolExecutor(max_workers=5)` in-process.
5. Results are merged and returned as a single response.

**Input schema (verbatim, KNOWN):**
```jsonc
{
  "tools": [{ "tool_slug": "GITHUB_CREATE_AN_ISSUE", "arguments": {...} }],
  "thought": "...",                       // one-sentence rationale
  "sync_response_to_workbench": true,     // REQUIRED; offloads large responses to sandbox FS
  "current_step": "FETCHING_EMAILS",      // progress telemetry, not execution semantics
  "current_step_metric": "10/100 emails", // "done/total units"
  "session_id": "..."
}
```

**Response shape — 4-level nesting (KNOWN verbatim):**
```jsonc
{ "tool_slug": "GITHUB_CREATE_AN_ISSUE", "index": 0, "error": null,
  "response": { "data": { "response": { "data": {
    "id":"123", "url":"https://github.com/..."
  } } }, "error": null, "successful": true, "log_id": "log_XYZ" } }
```
So: `result.response.data.response.data.<fields>` — a 4-level nest. This is why the recipe prompt includes rule 20: `data = result.get("data", {}); if "data" in data: data = data["data"]`.

**Source:** `COMPOSIO_R4_ROUTER.md` R4.0 (cap correction), R4.2 (schema), R5.1 (responseSchema with nesting example).

---

## 3. Execution envelope — `{data, error, successful}`

Every tool execution — remote app tools, custom tools, meta-tools, and session execute — returns the same shape:

```python
# tools.py:84-87  (Python TypedDict)
class ToolExecutionResponse(TypedDict):
    data: dict
    error: str | None
    successful: bool
```

**How it is built:**
- Remote tools: `client.tools.execute(...)` response `.model_dump(exclude={"log_id","session_info"})`. SDK strips `log_id`/`session_info` from the response before returning. (KNOWN — `tools.py:645-650`.)
- Custom session tools: `{data, error, successful}` assembled by hand in `execute_custom_tool` (`custom_tool_execution.py:43-79`); Pydantic errors → `{data:{}, error:"Input validation failed: …", successful:False}`.
- TS divergence: TS **keeps** `logId`/`sessionInfo` in the returned response; Python strips them. (`COMPOSIO_R4_TOOLS.md` R4.13 divergence ledger.)

Meta-tool responses share `{ data: { [k: string]: unknown }, error: string | null, log_id: string }` (`SessionExecuteMetaResponse`, `session.d.ts:1363`). The `log_id` at this level is the meta-tool execution ID, distinct from per-sub-tool `log_id` inside `data`.

**Source:** `COMPOSIO_R4_TOOLS.md` R4.1 step 5; `COMPOSIO_R4_ROUTER.md` R4.1.

---

## 4. Deferred tool search — the two-stage schema load

Search returns partial tool entries; full schemas are lazy-loaded. The discriminator in the search response is `schemaRef` vs `input_schema`:

```ts
interface ToolSchemas {
  tool_slug; toolkit; description?;
  hasFullSchema?: boolean;            // true → input_schema is inlined
  input_schema?: {...};               // only when hasFullSchema
  schemaRef?: {
    tool: 'COMPOSIO_GET_TOOL_SCHEMAS';
    args: { tool_slugs: string[] };   // agent must call this to hydrate
  };
}
```
(KNOWN — `session.d.ts:1934`.)

`GET_TOOL_SCHEMAS` input schema:
```jsonc
{ "tool_slugs": ["GMAIL_SEND_EMAIL","SLACK_SEND_MESSAGE"],
  "include": ["input_schema"],   // default; add "output_schema" for workbench use
  "session_id": "..." }
```

**Built-in typo recovery:** on a slug miss the server returns `not_found[]` plus `suggestions` (up to 3 similar slugs per miss). (KNOWN — `COMPOSIO_R4_ROUTER.md` R5.1, `GET_TOOL_SCHEMAS.responseSchema`.)

The server's BM25 match key is the **`description`** field of each tool/toolkit (NOT the slug). This is confirmed by the repeated doc comment: *"Used for BM25 search matching and shown to the LLM"* (`session.d.ts:3940, :3965, :2427`). BM25 k1/b/analyzer remain **server-side black boxes**. The `related_tool_slugs` are powered by a separate **dependency-graph service** (`COMPOSIO_GET_DEPENDENCY_GRAPH` — "Composio Labs dependency graph API" — co-occurrence/prerequisite edges between tools). Not BM25. (KNOWN — `COMPOSIO_R4_ROUTER.md` R5.2.)

**Source:** `COMPOSIO_R4_ROUTER.md` R4.2–R4.3, R5.1–R5.2.

---

## 5. Modifier engine — 5 types, composition rules

```python
# _modifiers.py — 5 modifier types
@schema_modifier       # (tool, toolkit, schema:Tool) → Tool       — runs at get()-time
@before_execute        # (tool, toolkit, params:ToolExecuteParams) → params
@after_execute         # (tool, toolkit, response) → response
@before_file_upload    # (ctx) → str|False                          — chain; False = abort
@before_execute_meta   # (tool, toolkit, session_id, params) → params  — tool-router path
@after_execute_meta    # (tool, toolkit, session_id, response) → response
```

**Execution order in `Tools.execute`** (CORRECTED from prior teardowns — this is the real order):
1. Auto file-upload (if enabled) — runs BEFORE `before_execute`
2. `before_execute` modifiers
3. Route: custom vs remote
4. Execute (network or in-process)
5. Auto file-download (if enabled)
6. `after_execute` modifiers

**Scoping** (`Modifier.apply`, `_modifiers.py:196-224`): if `tools` and `toolkits` are both empty → applies to ALL tools. Else applies only if `tool ∈ self.tools OR toolkit ∈ self.toolkits`. Modifiers are folded left-to-right.

**`before_file_upload` chaining** (`merge_before_file_upload`): each hook's returned path feeds the next; any `False` aborts the entire chain. Legacy 3-arg `(path, tool, toolkit)` callables are auto-adapted via param-count introspection.

**`ToolExecuteParams` carrier TypedDict** (`_modifiers.py:13-24`): `arguments` (required) + optional `allow_tracing, connected_account_id, custom_auth_params, custom_connection_data, entity_id, text, user_id, version, dangerously_skip_version_check`. A `before_execute` modifier can rewrite ANY of these, not just `arguments`.

**TS divergence:** TS modifiers are plain functions on a `modifiers` object; no declarative scoping lists. User must branch on `toolSlug` inside the function body. Python scopes declaratively (`tools=[...], toolkits=[...]`).

**Source:** `COMPOSIO_R4_TOOLS.md` R4.7; execute order correction at R4.1 step 2–3.

---

## 6. Execute path — full ordered pipeline (Python)

`Tools.execute(slug, arguments, ...)` (`tools.py:653-795`):

1. **Schema cache** — `self._tool_schemas[slug]` (unbounded dict, no TTL, no eviction). On miss: check custom-tool registry; on miss again: one network fetch + cache.
2. **Auto file-upload** — before execute modifiers. (CORRECTED order.)
3. **`before_execute` modifiers** — can rewrite `connected_account_id`, `version`, `user_id`, `dangerously_skip_version_check`, and more, not just `arguments`.
4. **Route** — custom vs remote.
5. **`_execute_tool`** (remote path):
   - `_serialize_arguments` recursively `model_dump(mode="json")` any Pydantic models in args.
   - Version resolution: `get_toolkit_version(toolkit_slug, self._toolkit_versions)`.
   - **Version guard**: if resolved version == `"latest"` and `dangerously_skip_version_check` is not True → **raises `ToolVersionRequiredError`**. All provider-injected execute paths hard-wire `dangerously_skip_version_check=True` to bypass this.
   - HTTP call: `POST /api/v3/tools/execute/{tool_slug}` via Stainless client. `None` → Stainless `omit` sentinel (field dropped from JSON, not sent as null).
   - Response: `.model_dump(exclude={"log_id","session_info"})` before returning.
6. **Auto file-download** (after remote execute).
7. **`after_execute` modifiers**.

**Three distinct execute entry points:**
- `Tools.execute` — public direct-execute API.
- `_wrap_execute_tool` — provider agentic path (via `functools.partial`, `dangerously_skip_version_check=True`).
- `_wrap_execute_tool_for_tool_router` — session path; calls `POST /api/v3.1/tool_router/session/{id}/execute`; hard-wires `enable_auto_workbench_offload=True`.

**Source:** `COMPOSIO_R4_TOOLS.md` R4.1.

---

## 7. Schema enhancement — always-on description augmentation

Before any provider sees the tools, `_get` (`tools.py:358-373`) mutates each tool's `input_parameters`:

```python
enhance_schema_descriptions(schema)  # _files.py:733-763
# For every primitive property (string/integer/number/boolean):
#   appends "Please provide a value of type <type>." to its description.
# For every property in `required`:
#   appends "This parameter is required."
```

This runs **regardless of file-handling settings** — it is always on. It's a prompt-engineering nudge baked into the schema the LLM sees.

**TS divergence:** TS SDK does NOT have `enhance_schema_descriptions`. Same tool yields different LLM-facing schema between Python and TS SDKs. (KNOWN — `COMPOSIO_R4_TOOLS.md` R4.9 point 5.)

---

## 8. Custom tool session system (the surviving one)

The legacy in-process registry (`composio.tools.custom_tool`) was deleted in June 2026 (Python `0.14.0`, TS `0.10.0`). One system remains: **experimental session-scoped tools**.

```python
@composio.experimental.tool()
def grep(input: GrepInput, ctx: SessionContext): ...
```

Key mechanics:
- Slug = `fn.__name__.upper()`; **prefix `LOCAL_`** mandatory. Max slug 60 chars after prefix. `LOCAL_` and `COMPOSIO_` prefixes reserved — registration rejects them in the raw slug.
- Input MUST be a named-field Pydantic `BaseModel` (RootModel rejected). `async def` rejected.
- Input validation at execute time via `handle.input_params.model_validate(arguments)` — unknown fields dropped (SEC-365 invariant: LLM cannot smuggle `user_id`/`tenant_id`).
- **Session sibling routing** (`ctx.execute(slug, args)`): checks local custom-tools map first; if a sibling local tool matches, runs it **in-process with no network round-trip**. This is a genuine constant-factor win for composite agent workflows.
- Wire serialization on each session call: `{slug, name, description, input_schema, [extends_toolkit], [preload]}`. Sent inline on every search/execute because v3.1 sessions don't persist inline custom tools.

**Source:** `COMPOSIO_R4_TOOLS.md` R4.5 System B, R5.0–R5.1.

---

## 9. Elicitation / permissions model

```ts
// session.d.ts:2524-2543
interface Permissions {
  default: 'allow_all' | 'ask_every_call' | 'ask_once_per_session';
  overrides?: {
    [key: string]: 'always_allow' | 'always_deny' | 'ask_once' | 'ask_always'
  };
}
```

**Override key format:** `"${toolSlug}:${connectedAccountId ?? '__none__'}"` for per-tool, `"*:${connectedAccountId ?? '__none__'}"` for account-wide wildcard. **Resolution order:** exact tool key > wildcard key > `default`.

`ask_always` bypasses even a cached session `allow_once`. On PATCH the entire `permissions` block is replaced (not merged).

This is a clean, reimplementable 4-way decision lattice. No external dependencies.

**Source:** `COMPOSIO_R4_ROUTER.md` R4.6.

---

## 10. Planner architecture — cracked

The planner is a cache in front of BM25:

```
SEARCH_TOOLS(use_case)
  → recipe-cache lookup (match key = use_case text)
     HIT:  returns plan_id + recommended_plan_steps + known_pitfalls + reference_workbench_snippets
           (no memory field)
     MISS: BM25 over tool/toolkit `description` → ~6 candidates
           → LLM re-rank/plan + COMPOSIO_CREATE_PLAN (for medium/hard tasks)
           → primary_tool_slugs + related_tool_slugs + difficulty
           (+ per-app memory of discovered IDs)
  → always: tool_schemas (partial, schemaRef) + toolkit_connection_statuses + time_info + session
```

`known_fields` (a comma-sep `"key:value"` string, NOT an array) feeds the plan/memory context but **does NOT pollute the BM25 query key**. This is intentional: keeps `use_case` as a clean lexical key for recipe-cache matching, while `known_fields` resolves IDs.

`COMPOSIO_CREATE_PLAN` output sections (KNOWN verbatim): `workflow_steps`, `complexity_assessment`, `decision_matrix`, `failure_handling`, `output_format`. Called only for medium/hard tasks; skipped for "easy" ones.

`related_tool_slugs` = output of `COMPOSIO_GET_DEPENDENCY_GRAPH` — a separate co-occurrence/prerequisite graph service ("Composio Labs dependency graph API"), not BM25. A reimplementer needs a tool→tool prerequisite index separate from the text search index.

**Source:** `COMPOSIO_R4_ROUTER.md` R4.3, R5.2.

---

## 11. HTTP retry/backoff — exact constants (corrected)

The legacy v0.7 "3 retries, no backoff" is DEAD. Current SDK (Stainless-generated):

```
maxRetries        = 2   (not 3)
initialRetryDelay = 0.5 s
maxRetryDelay     = 8.0 s
backoff           = min(0.5 × 2^numRetries, 8.0)  ×  [0.75, 1.0) jitter
retryOn           = 408, 409, 429, ≥500  (or x-should-retry header)
retry-after-ms    honored before backoff math
DEFAULT_TIMEOUT   = 60 s
```

**Source:** `COMPOSIO_R4_CORE.md` R4.5 (Stainless `client.ts:290, 643-720`).

---

## 12. Wire error envelope

```json
{ "error": { "code": 10401, "slug": "HTTP_Unauthorized",
             "status": 401, "message": "No authentication provided",
             "request_id": "f8702...", "suggested_fix": "..." } }
```

SDK clients should branch on **`slug`** (stable string enum), not `message` (prose, may change). The `code` field is internal (10xxx), not an HTTP status. `request_id` is a UUIDv4 generated server-side, present in the error envelope itself, not just response headers.

40-class SDK error taxonomy all prefixed `TS-SDK::` (e.g. `TS-SDK::NO_API_KEY_PROVIDED`). Branch point for tool execution: backend numeric code `1803` → `ComposioConnectedAccountNotFoundError`.

**Source:** `COMPOSIO_DEEP.md` §2.1; `COMPOSIO_R4_CORE.md` R4.10.

---

## 13. Session OCC + config history

`config_version` is monotonically incremented on every PATCH. Prior config is snapshotted into `configHistory` (accessible via `GET .../configHistory`, returned DESC with `is_current:true/false`). PATCH is partial: omit a field to preserve it; pass `null` to clear `manage_connections`/`workbench`/`multi_account`. **On PATCH, the entire `permissions` block is replaced, not merged.**

The SDK exposes `config_version` to *detect* concurrent updates (stale local copy), not as a client-sent precondition. Last-writer-wins with version-bump + history snapshot is the server behavior.

**Source:** `COMPOSIO_R4_ROUTER.md` R4.8.

---

## 14. Provider adapter pattern — the clean abstraction

Two provider families:

- **NonAgenticProvider** (`OpenAI`, `Anthropic`, `Google Vertex`, `OpenAIResponses`) — wraps tools as framework type objects; SDK executes when developer calls `handle_tool_calls()`.
- **AgenticProvider** (`LangChain`, `LangGraph`, `CrewAI`, `Gemini`, `GoogleADK`, `OpenAIAgents`, `ClaudeAgentSDK`, `AutoGen`, `LlamaIndex`) — wraps tools with `execute_tool` baked in; framework calls back the tool directly.

Every provider receives the same canonical descriptor: `{slug, description, input_parameters (JSON Schema), output_parameters}`. The transformer's job is purely mapping that JSON Schema to the target framework's tool type. Most per-provider complexity is schema sanitization (each model API has different acceptability rules):

| Provider | Schema munge |
|---|---|
| OpenAI, Anthropic | verbatim (zero munge) |
| Google Vertex (Py) | strips `examples` key (Vertex rejects it) |
| OpenAIAgents (Py) | `additionalProperties:false` + strips `examples/pattern/default` + injects `items.type="string"` for typeless arrays |
| VercelAI, MastraAI (TS) | `removeNonRequiredProperties` in strict mode |
| MastraAI | `dereferenceJsonSchema` to inline all `$ref` (AJV refuses unresolved `$ref`) |
| LangChain, AutoGen, Gemini (Py) | reserved-keyword substitution (`class`→`class_rs`) |
| AutoGen | 64-char function-name hash: `<last 53 chars of slug>_<first 10 chars of sha256(slug)>` |

`ClaudeAgentSDKProvider` is a structurally different output: each tool becomes an MCP tool via `@sdk_tool(slug, description, input_schema)` decorator; the wrapper returns an in-process MCP server, not a wrapped function.

**Source:** `COMPOSIO_R4_TOOLS.md` R4.2–R4.4, R4.9–R4.10.

---

## 15. Workbench helpers — exact signatures (KNOWN verbatim)

```python
# All helpers preloaded — do NOT import or redeclare
run_composio_tool(tool_slug: str, arguments: Dict[str, Any]) -> tuple[Dict[str, Any], str]
  # success: ({"data": {...}}, "")    failure: ({}, "error message")
  # NEVER call COMPOSIO_* meta tools here (cycle risk)

invoke_llm(query: str) -> tuple[str, str]
  # max 200k chars input
  # recipe variant adds reasoning_effort: "low"|"medium"|"high"

upload_local_file(*file_paths) -> tuple[Dict[str, Any], str]
  # success: ({"s3_url": str, "uploaded_file": str, "type": str, "id": str, "s3key": str, "message": str}, "")
  # multiple files → auto-zipped

proxy_execute(method: Literal["GET","POST","PUT","DELETE","PATCH"],
              endpoint: str, toolkit: str,
              query_params=None, body=None, headers=None) -> tuple[Any, str]
  # exactly ONE toolkit per call

web_search(query: str) -> tuple[str, str]   # Exa AI backend (verbatim)

smart_file_extract(sandbox_file_path: str, show_preview: bool = True) -> tuple[str, str]
  # PDF/image text extraction
```

Three concurrency numbers at three layers: **50** (server multi-execute cap) · **10** (in-workbench `ThreadPoolExecutor` recommended example) · **5** (SDK local fan-out `MAX_PARALLEL_WORKERS`).

Filesystem: `/home/user` (volatile — lost on sandbox recreation) · `/mnt/files/` (cloud FUSE, persists across failures/timeouts/restarts). Truncated artifacts land at `/mnt/files/.composio/output`.

**Source:** `COMPOSIO_R4_ROUTER.md` R4.2 (workbench description), R5.1 (verbatim signatures).

---

## 16. Hype / tactical-hack flags

- **The 10K+ tools catalog** — genuinely durable. Every tool is data (`{slug, description, input_schema}` stored server-side); the proxy endpoint (`POST /api/v3/tools/proxy`) is the universal execution primitive. Tools don't require new code.
- **Recipe/plan caching** — durable mechanism, but the match metric (slug vs embedding), recipe runtime, and memory TTL are server-side black boxes. A reimplementer must design this from scratch.
- **The per-provider transformer matrix** (~23 providers) — purely an integration tax driven by each framework's idiosyncratic type system. **Depreciating tactical:** as frameworks standardize on OpenAI function-calling shape (which Anthropic and Google now also support), the per-provider munge becomes unnecessary. Plan for most transformers to converge on the verbatim passthrough.
- **`enhance_schema_descriptions`** — a pure prompt-engineering nudge appended to JSON Schema. Model-generation-dependent; may help now, likely irrelevant at next-generation model capability. Tactical, depreciating.
- **`x-runtime` fingerprinting** (17 × 4 = 68 possible tags) — product analytics / support signal, not execution logic. Zero value for a Lancelot agent layer.

---

## 17. Lancelot byte-path implications

| Composio primitive | Lancelot status |
|---|---|
| **MULTI_EXECUTE batch dispatch** (`ThreadPoolExecutor(max_workers=5)` + single backend call for remote) | `TransportPool.fanout(coros, first_n=, deadline=, cancel_laggards=True, concurrency=)` is the Lancelot analog for the remote batch call. The local-vs-remote routing split is an agent-layer decision; Lancelot's byte path handles the outbound fan-out. No rebuild needed. |
| **`{data, error, successful}` execution envelope** | Not provided. Lancelot returns raw HTTP responses; the agent layer needs to define and enforce this envelope shape. Clean, small addition to the agent loop. |
| **Deferred tool search (BM25 + schemaRef lazy load)** | Not provided. This is pure agent logic. Lancelot's `@app.tool` and `lancelot.mcp.mount` expose tools; the search/discovery layer (BM25 index, schemaRef indirection) is the agent half. |
| **Modifier engine (5 types, before/after/schema)** | Not provided. The agent layer needs this. Maps cleanly to ASGI-seam middleware: `before_execute` ~ request interceptor, `after_execute` ~ response interceptor, `schema_modifier` ~ registration-time transformer. Lancelot's `@app.middleware("http")` is the wrong level (HTTP, not tool-call level). |
| **Permissions / elicitation (allow_all / ask_once / ask_every_call)** | Not provided. Clean 4-way decision lattice; needs to live in the agent loop with a per-session state store. |
| **Session OCC (config_version + history)** | Not provided. Durable seam / checkpoint: maps to `lancelot.context` ContextVars for current state; persisting history requires the durable-seam (event-sourced checkpoint) that is the agent layer's job. |
| **`run_composio_tool` workbench helper** | Directly replaced by `TransportPool.request()` + the tool execution envelope. The workbench helper is a convenience wrapper over exactly the same HTTP call. |
| **Retry/backoff (maxRetries=2, 0.5→8s ×[0.75,1.0) jitter, retry-after honoring)** | `TransportPool` has exp-backoff + Retry-After honoring. Exact constants differ. No rebuild needed; verify policy matches if exact parity required. |
| **`x-request-id` + `traceparent` injection** | `TransportPool` auto-injects `x-request-id` + `traceparent`. **Lancelot already does what Composio's Python SDK does here.** |

**Key gaps to fill in the Lancelot agent layer (from Composio evidence):**
1. `{data, error, successful}` envelope — trivial TypedDict-equivalent; 4 lines.
2. Tool-call modifier engine — before/after/schema hooks with per-tool/toolkit scoping. Map to a `ModifierChain` that wraps the agent's tool dispatch.
3. Deferred schema load + BM25 search — tools are already registered via `@app.tool`. Need a text search index over tool descriptions (sqlite FTS5 for simplicity) + `schemaRef` lazy-hydration pattern.
4. Permissions / elicitation — 4-way decision lattice + per-session override map. Small state machine.
5. Session sibling routing (custom tool A calls custom tool B in-process) — relevant for the agent loop's local tool dispatch.
