# Vercel AI SDK + v0 — Mechanism-level teardown for Lancelot

> Sources: `VERCEL_AI_SDK.md` (Tier-1A, `vercel/ai` monorepo clone 2026-05-30) and `V0_VERCEL.md` (Tier-1A, leaked system prompt 989L + Tools.json 326L, 2026-05-19).
> Focus per brief: `generateText`/`streamText`/`generateObject`, tool-calling loop (`maxSteps`/`stopWhen`), the streaming protocol + UI message parts, provider registry. Copy `stopWhen` + structured stream-part model.

---

## 1. The agent loop — the `do…while` with `stopWhen` (highest priority)

Source: `VERCEL_AI_SDK.md §1.1`, `generate-text/generate-text.ts:228,662-1181`

### 1.1 Exact default and loop contract

```python
# Python translation of the TypeScript semantics

DEFAULT_STOP = stepCountIs(1)   # single LLM call unless you opt into agentic mode

steps = []
do:
    result = prepareStep(model, steps)      # per-step overrides
    response = model.doGenerate(...)        # or doStream
    tool_calls = parse_tool_calls(response)
    approval_gate(tool_calls)               # HITL if needed
    results = execute_tools(tool_calls)
    steps.append(StepResult(...))
while (
    (client_tool_calls_exist and all_resolved_or_denied)
    or deferred_pending
) and not stop_condition_met(stop_conditions, steps)
```

Key semantics (KNOWN, `generate-text.ts:1171-1181`):
- The loop continues **only when the previous step produced tool calls** that have all been resolved or denied. No tool calls → model emitted final text → stop. This is the correct "don't loop unless there's work to do" guard.
- `stopWhen` is `Arrayable<StepCondition>` — OR semantics: any true condition fires. `isStopConditionMet` returns `.some()` (`:1180`).

### 1.2 Stop conditions — the exact API

`VERCEL_AI_SDK.md §1.2`, `generate-text/stop-condition/step-count-is.ts`

```python
# Built-in stop conditions:
stepCountIs(n)        # stop after n total steps (replaces old `maxSteps`)
hasToolCall('name')   # stop once a named tool was called
# Custom: any callable(steps) -> bool | awaitable[bool]
```

The `ToolLoopAgent` (the real exported `Experimental_Agent`) overrides the raw `generateText` default from `stepCountIs(1)` → **`stepCountIs(20)`** as its own default (`agent/tool-loop-agent.ts:125`, CE-3 in source). This is the right design for an agent class: single call by default at the primitive level, 20-step default at the agent class level.

**Lancelot implication**: adopt exactly this two-layer default:
- `generate()` primitive: `stop_when=stepCountIs(1)` — must explicitly opt into looping.
- `Agent` class: `stop_when=stepCountIs(20)` default.
- The stop condition composability (AND via array, OR semantics) is clean; copy it.

### 1.3 `prepareStep` — the single most powerful agentic hook

`VERCEL_AI_SDK.md §1.3`, `generate-text.ts:677`, `prepare-step.ts:172`

Signature: `prepareStep({model, steps, stepNumber, instructions, messages, responseMessages, runtimeContext, toolsContext}) -> {model?, instructions?, activeTools?, toolChoice?, providerOptions?, ...}`

Use cases this enables WITHOUT subclassing the loop:
- Switch to a cheaper model on steps > 2.
- Force `toolChoice='required'` on step 0, relax on later steps.
- Shrink `activeTools` as the task narrows (fewer schema tokens on each step).
- Compact `messages` (call a pruner) when context grows.
- Inject retrieved memories on step 0, prune them on step N.

This is the correct hook: it runs once per step with full loop state visibility, returns overrides only, and the caller never sees the loop internals. Lancelot should copy this exactly as a `prepare_step` async callback.

### 1.4 Tool approval / HITL

`VERCEL_AI_SDK.md §1.4`, `generate-text.ts:891-1003`

The mechanism: a tool emits `tool-approval-request` (with a generated `approvalId`) instead of executing. The loop's continue condition counts denied approvals toward `allResolved` (`:1001`) — so the model sees the denial ("execution denied") as a tool result on the next step. No special second pass needed; the denial becomes a regular message in the tool-result chain.

**Note**: per-tool `needsApproval` is **deprecated** (CE-4) in favor of loop-level approval config. The loop-level config takes precedence.

---

## 2. Provider normalization — `LanguageModelV2` (the translation primitive)

Source: `VERCEL_AI_SDK.md §2`, `packages/provider/src/language-model/v2/language-model-v2.ts:14`

### 2.1 The spec interface

```python
class LanguageModelV2(Protocol):
    specification_version: Literal['v2']
    provider: str       # e.g. 'openai.chat'
    model_id: str
    supported_urls: dict[str, list[re.Pattern]]  # URLs the model fetches natively → skip download

    def do_generate(self, options: CallOptions) -> GenerateResult: ...
    def do_stream(self, options: CallOptions) -> StreamResult: ...
```

`do_` prefix is deliberate ("prevent accidental direct usage by the user", `:49`). Users call `generate_text`/`stream_text`, never `model.do_generate` directly. This is the correct ergonomic choice — Lancelot should mirror it.

### 2.2 Ordered content parts — the V1→V2 breakthrough

V1 returned `{text, toolCalls, reasoning}` as separate top-level fields. V2 returns `content: list[Text | Reasoning | File | Source | ToolCall | ToolResult]` — **order-preserving** (a model can emit text, then a tool call, then more text — all in sequence). This is what enables reasoning models (o1, Claude thinking) and interleaved tool calls to work uniformly.

**Lancelot should adopt the ordered content list.** Not separate fields.

### 2.3 Normalized `finishReason`

`language-model-v2-finish-reason.ts`: `'stop' | 'length' | 'content-filter' | 'tool-calls' | 'error' | 'other' | 'unknown'`. Each provider maps its native reason to this 7-value enum; the raw string is preserved separately as `rawFinishReason`. The loop reasons over the normalized `unified` value.

### 2.4 Provider stream parts — exact union (16 types)

`language-model-v2-stream-part.ts:11` (KNOWN):
```
stream-start
text-start / text-delta / text-end
reasoning-start / reasoning-delta / reasoning-end
tool-input-start / tool-input-delta / tool-input-end
tool-call
tool-result
file
source
response-metadata
finish          ← carries usage + finishReason
raw             ← passthrough for provider-specific data
error
```

Each provider package's job: translate its native SSE → this union. Then `streamText` is provider-agnostic. The start/delta/end triples enable **overlapped tool execution**: `streamText` kicks off `tool.execute` as soon as `tool-input-end` arrives, overlapping execution with continued generation (`execute-tools-from-stream.ts:219`).

### 2.5 `providerOptions` passthrough

`LanguageModelV2CallOptions` carries a typed `providerOptions` dict for provider-specific knobs (Anthropic `cacheControl`, OpenAI `reasoningEffort`) that pass through unchanged. The response has `providerMetadata` for the same direction. This is the clean escape hatch — the normalized interface doesn't need to enumerate every provider feature.

### 2.6 Middleware wrapping

`wrapLanguageModel({model, middleware})` — intercepts `doGenerate`/`doStream` for any model uniformly. Built-in uses: `extractReasoningMiddleware` (parse `<think>` tags), `simulateStreamingMiddleware`, `defaultSettingsMiddleware`, logging/caching. This is the Lancelot analog of ASGI middleware, but at the LLM-call level.

---

## 3. Streaming protocol — two layers (provider and UI)

Source: `VERCEL_AI_SDK.md §7`, `ui-message-stream/ui-message-chunks.ts`, `process-ui-message-stream.ts`

### 3.1 The two-layer design

Layer 1: **provider stream** — the 16-part `LanguageModelV2StreamPart` union (server-internal). Layer 2: **UI Message Stream** — the 27-chunk `TextStreamPart` protocol served to the browser client via SSE.

The server calls `.toUIMessageStreamResponse()` which converts provider stream → SSE of JSON data parts. The client `useChat` transport decodes them and reconstructs `UIMessage.parts[]` incrementally.

### 3.2 The 27 UI chunk types (KNOWN, `ui-message-chunks.ts`)

```
start, finish, abort, error,
start-step, finish-step,
text-start, text-delta, text-end,
reasoning-start, reasoning-delta, reasoning-end, reasoning-file,
source-url, source-document,
file,
tool-input-start, tool-input-delta, tool-input-available, tool-input-error,
tool-output-available, tool-output-error, tool-output-denied,
tool-approval-request, tool-approval-response,
message-metadata,
custom          # data-* parts via writer.write({type:'data-foo', data})
```

Wire format: `data: {"type":"text-delta","id":"...","delta":"..."}\n\n`. Terminated with `data: [DONE]`.

**`custom`/`data-*` parts** are how you stream arbitrary structured data (status, sources, generative-UI props) to the client mid-generation. `transient:true` parts stream but are not persisted into the final message.

### 3.3 Client-side reconstruction state machine

`process-ui-message-stream.ts` (KNOWN). Threads `StreamingUIMessageState = {message, activeTextParts: dict[id, TextUIPart], activeReasoningParts, partialToolCalls: dict[toolCallId, ...]}`. Per-chunk transitions:

- `text-start` → push `TextUIPart{state:'streaming'}`, register in `activeTextParts[id]`. `text-delta` → look up by id, append delta (hard error if missing — "`text-start` MUST precede `text-delta`"). `text-end` → finalize, remove from active.
- Tool calls: 4-state machine per `toolCallId`: `tool-input-start → input-streaming → input-available → output-available | output-error`. The client knows the exact phase for rendering progress.
- `custom`/`data-*` → generative-UI parts; `transient:true` → rendered only, not persisted.

**Why this matters for Lancelot**: the strict start/delta/end ordering with per-id tracking is what makes streaming composable. A dropped `text-start` is a hard error — this enforces correct emitter behavior. Lancelot's SSE protocol should adopt this structure if it wants to support progressive UI rendering with tool call visibility.

### 3.4 `streamText` chunk timeout (more granular than step timeout)

`stream-text.ts:1568,1578` (KNOWN): `streamText` has both a per-step timeout (`stepTimeoutMs`) AND a **per-chunk timeout** (`chunkTimeoutMs`) that resets on every received chunk. If the provider stalls mid-stream, the chunk timeout aborts before the step timeout would fire. This is more granular than `generateText`'s step-only timeout and is important for LLM streaming where partial generation stalls are common.

**Lancelot already has**: `header_read_timeout` (slow-loris), per-route timeout. The per-chunk streaming timeout is the missing piece for the streaming path.

---

## 4. Tool contract — the full taxonomy

Source: `VERCEL_AI_SDK.md §3`, `CE-4`, `provider-utils/src/types/tool.ts:80-169`

### 4.1 The `tool()` factory fields

```python
tool(
    input_schema=...,           # Zod / Valibot / raw JSON Schema
    output_schema=...,          # optional; validates result; required when no execute
    execute=...,                # (input, {tool_call_id, messages, abort_signal, context}) -> output
    needs_approval=...,         # DEPRECATED at tool level; use loop-level approval
    on_input_start=...,         # fires as model streams arguments (before exec)
    on_input_delta=...,         # per arg-token during streaming
    on_input_available=...,     # fires when full input is parsed (even without execute)
    to_model_output=...,        # (tool_call_id, input, output) -> what the model sees next turn
    type=...,                   # 'function' (default) | 'dynamic' | 'provider-defined-server' | 'provider-defined-client'
)
```

### 4.2 `toModelOutput` — the result-shaping hook

`tool.ts:149` (KNOWN): maps raw tool output to what the model sees on the next turn. If absent, sends result as JSON. Use case: a tool returns 50KB of data; `toModelOutput` summarizes it to one sentence before feeding it back. Runs inside `convertToModelMessages`, so the **same tools dict must be passed to both the loop and the message conversion**.

This is the correct place to prevent context explosion from large tool results — not truncation, but explicit shaping by the tool author.

### 4.3 Three execution sites

1. **function tool** (default): `execute` runs in your process.
2. **`dynamicTool()`**: schema is `unknown` at type-level; for runtime-defined tools. Split into `staticToolResults`/`dynamicToolResults` in the final aggregate.
3. **provider-defined-server**: `providerExecuted:true`; runs on the provider side (web search, code exec). May set `supportsDeferredResults` — result arrives a later turn, tracked in `pendingDeferredToolCalls` map. The loop-continue condition explicitly handles deferred pending (`generate-text.ts:1070-1093`).

### 4.4 `toolsContext` (a.k.a. `experimental_context`)

A typed DI value threaded into every `execute` call. Mutable only via `prepareStep`. This is the clean channel for injecting per-step context (memory reads, tenant info, request-id) without polluting the tool's input schema.

---

## 5. `generateObject` — structured output strategies

Source: `VERCEL_AI_SDK.md §4`, `CE-1`, `CE-2`, `generate-object/output-strategy.ts:392-410`

### 5.1 Four output strategies

```python
output='object'     # validates partial+final against schema; default for object schema
output='array'      # wraps in {"elements": [...]}, streams element-by-element
                    # last element suppressed until isFinalDelta (avoids emitting incomplete trailing element)
output='enum'       # wraps in {"result": str}; streaming prefix-filters enum values as tokens arrive
                    # commits when exactly one candidate remains
output='no-schema'  # free-form JSON, no validation
```

**Array mode trick** (KNOWN, `output-strategy.ts:139-141`): the wrapper `{elements: [...]}` exists because "most LLMs will not be able to generate an array directly." The delta arithmetic: `textDelta` emits only new elements since `publishedElementCount`, prefixes `[`, joins with `,`, closes `]` on final.

**Enum mode trick** (KNOWN, `:358-377`): `possibleEnumValues = enumValues.filter(v => v.startsWith(partial.result))` — prefix-filter narrows to one candidate without waiting for the closing `"`.

### 5.2 Schema repair — the one-shot retry

`parse-and-validate-object-result.ts:77-111` (KNOWN):
```python
try:
    return parse_and_validate(text)
except (JSONParseError, TypeValidationError) as e:
    if repair_text is not None:
        repaired = await repair_text(text=text, error=e)
        if repaired is None:
            raise
        return parse_and_validate(repaired)   # exactly ONE re-parse
    raise
```

One retry only. `repair_text` is user-supplied. Fires only on JSON parse or type validation errors (not content-filter). Error carries `{text, cause, response, usage, finishReason}` for post-mortem. `streamObject` does `parsePartialJson` on each accumulated text delta, running the strategy's `validatePartialResult`.

---

## 6. Registry + gateway

Source: `VERCEL_AI_SDK.md §4`, `CE-6`, `registry/provider-registry.ts:132-199`

### 6.1 String-routable model IDs

`createProviderRegistry(providers, {separator=':'})` resolves `'openai:gpt-4o'` → `providers['openai'].languageModel('gpt-4o')`. Default separator is `':'` (`:143`). `customProvider({languageModels, fallbackProvider})` aliases models (`'fast' → openai('gpt-4o-mini')`); unknown IDs fall through to `fallbackProvider` or raise `NoSuchModelError`.

`resolveLanguageModel(id|model)` accepts either a string (registry lookup) or a `LanguageModelV2` directly — so callers can pass either form uniformly.

### 6.2 The gateway IS a `LanguageModelV2`

The AI Gateway (`@ai-sdk/gateway`) implements `LanguageModelV2` — `languageModel(id)` returns a `GatewayLanguageModel` that POSTs to Vercel's hosted endpoint. So `createProviderRegistry({gateway}) + 'gateway:anthropic/claude-...'` routes through one key. The loop sees it as just another model. **The provider spec is the right abstraction level**: a gateway, a local Ollama, and OpenAI all look identical to the loop.

---

## 7. v0-specific mechanisms (from `V0_VERCEL.md`)

### 7.1 `taskNameActive`/`taskNameComplete` — every tool carries its own UI labels

Every one of v0's 10 tools requires `taskNameActive` and `taskNameComplete` — 2-5 word labels the model emits as part of the tool call, shown in the UI while the tool runs and when it completes. The **model controls its own progress UI** by emitting these strings as part of the tool schema. This is a generalized version of the `message-metadata` part in the UI Message Stream.

Lancelot implication: a clean pattern for tool UI is to include `label_active` / `label_done` as optional fields in the tool's input schema, and the framework propagates them as a `message-metadata` stream part automatically.

### 7.2 Lazy tool loading — `ToolSearch` as a meta-tool

`V0_VERCEL.md §17` (KNOWN): the initial `Tools.json` has only 10 tools. Write/Edit/Move/Delete/Bash/Skill/ToolSearch are loaded dynamically. `ToolSearch` is a meta-tool that loads MCP tool schemas on demand. **The initial tool set is small; tools are discovered at runtime.** This is the same pattern Claude Code uses.

Lancelot implication: the agent runtime should support a `tool_search` primitive that takes a query and returns tool schemas. This avoids burning context budget on 200 tool schemas when the agent will use 5. Lancelot's `mcp.mount` already takes a first step here (MCP server as lazy tool registry), but the agent loop needs a `lookup_tools(query)` primitive.

### 7.3 Memory architecture — index-file + topic files, no embeddings

`V0_VERCEL.md §7` (KNOWN, prompt lines 863-908):
- `MEMORY.md` always in context, **truncated at ~200 lines**. Used as an index linking to topic files.
- Topic files (`debugging.md`, `patterns.md`) loaded on demand via Read tool.
- Two scopes: `user/` and `team/`.
- No vector DB, no embedding. The model routes to topic files via filename heuristics.

This is the simplest persistent memory that actually works. The 200-line truncation is the key constraint — keeps it always-loaded but forces the agent to organize hierarchically. Compare to Letta's vector-store archival memory (more powerful but more complex).

Lancelot's agent memory should at minimum support this pattern: an always-loaded index file + on-demand topic files, managed by the agent itself via standard file tools.

### 7.4 Context compression (tool result summaries)

`V0_VERCEL.md §2, point 4` (KNOWN, prompt lines 71-77): "tool results from previous turns may be compressed... replaced with a short summary." The agent is taught to re-fetch when needed. This is how v0 handles long conversations — **aggressive context compression with a re-fetch fallback**.

Lancelot's context compaction primitive needs: (a) ability to mark a past tool result as "summarized + re-fetchable", (b) the agent instruction to re-run the tool when it needs the full result. The re-fetch fallback prevents compaction from being lossy in practice.

### 7.5 `SearchRepo` — the sub-agent tool pattern

`V0_VERCEL.md §1.8` (KNOWN): `SearchRepo` "launches a new agent that searches and explores the codebase using multiple search strategies." The magic-string trigger `"Give me an overview of the codebase"` fires a pre-tuned exploration path. This is agent-as-tool: the tool's `execute` spawns a full sub-agent with its own context window. Lancelot's `TransportPool.spawn_child` is the byte-path substrate; the agent layer needs a `subagent_tool()` factory that wraps it.

### 7.6 The `automated_v0_instructions_reminder` pattern — runtime instruction injection

`V0_VERCEL.md §11` (KNOWN, prompt lines 65-69): the backend injects `automated_v0_instructions_reminder` messages mid-conversation to change agent behavior silently. The agent is conditioned not to respond to them. This is the operator control plane — Lancelot's equivalent would be a special message type (not from the user, not from the assistant) that the loop consumes as instruction overrides without treating as conversational content.

---

## 8. Smooth streaming — the UI chunking transform

Source: `VERCEL_AI_SDK.md §1.5`, `smooth-stream.ts:9,31`

`smoothStream({chunking, delayInMs})` is an optional transform applied before emitting `text-delta` parts:
- `chunking='word'` (default): regex `/\S+\s+/m` — emit on word boundaries.
- `chunking='line'`: `/\n+/m`.
- `chunking=RegExp` or `Intl.Segmenter` (for CJK).
- `delayInMs=10` default (or `null` to disable).

This re-chunks the raw LLM text stream into word-by-word deltas for smooth UI animation, independent of how the provider chunks (which is arbitrary and often character-by-character or large). Lancelot's `@app.stream` pump delivers raw chunks from the generator; a `smooth_stream` wrapper transform before the SSE emit would be a clean addition.

---

## 9. Exact constants table (all KNOWN, cited)

| Constant | Value | Source |
|---|---|---|
| `generateText` default `stopWhen` | `stepCountIs(1)` (single call) | `generate-text.ts:228` |
| `ToolLoopAgent` default `stopWhen` | `stepCountIs(20)` | `tool-loop-agent.ts:125` |
| stop condition evaluation | OR semantics (`.some()`) | `generate-text.ts:1180` |
| loop-continue condition | all client tool calls resolved/denied OR deferred pending, AND !stop | `generate-text.ts:1171-1181` |
| `smooth-stream` `delayInMs` | 10 | `smooth-stream.ts:31` |
| `smooth-stream` default chunking | `'word'`, regex `/\S+\s+/m` | `smooth-stream.ts:9,32` |
| `generateObject` modes | object / array / enum / no-schema | `output-strategy.ts:392-410` |
| schema repair | exactly ONE retry, user-supplied `repair_text` fn | `parse-and-validate-object-result.ts:77-111` |
| provider registry separator | `':'` default | `provider-registry.ts:143` |
| provider spec | `LanguageModelV2` (`v2`, v3, v4 in tree) | `language-model/` |
| `finishReason` enum | stop/length/content-filter/tool-calls/error/other/unknown (7 values) | `language-model-v2-finish-reason.ts` |
| provider stream parts | 16 types | `language-model-v2-stream-part.ts:11` |
| UI message chunks | 27 types | `ui-message-chunks.ts` |
| per-step timeout | `stepTimeoutMs` | `generate-text.ts:664` |
| per-chunk timeout (streamText only) | `chunkTimeoutMs`, resets per chunk | `stream-text.ts:1578` |
| v0 `MEMORY.md` truncation | ~200 lines | v0 prompt line 879 |
| v0 `ReadFile` small-file threshold | 2,000 lines | v0 Tools.json |
| v0 tool count (initial) | 10 (dynamic tools loaded via ToolSearch) | v0 Tools.json |
| v0 integration enum size | 12 hard-coded | v0 Tools.json |

---

## 10. What to copy vs. what to skip

### Copy (mechanism-level)

1. **`stopWhen` composability** — `Arrayable[StopCondition]`, OR semantics, built-ins `stepCountIs(N)` and `hasToolCall(name)`. Two-layer default: primitive=1, Agent=20. (`generate-text.ts:228,1180`)

2. **`prepareStep` hook** — fires per-step, receives full `steps[]` + current config, returns overrides only. The single hook that handles model-switching, tool-narrowing, context-pruning, memory injection. (`prepare-step.ts:172`)

3. **Ordered content parts** — `content: list[Text|Reasoning|File|Source|ToolCall|ToolResult]` preserving interleave order. No more `{text, toolCalls}` split. (`language-model-v2-content.ts`)

4. **Provider stream part union** — the 16-type start/delta/end triple pattern, especially `tool-input-start/delta/end` enabling overlapped tool execution. (`language-model-v2-stream-part.ts`)

5. **UI Message Stream state machine** — 27-chunk SSE protocol with per-id active part tracking, 4-state tool-call machine. `text-start` MUST precede `text-delta` or hard error. (`process-ui-message-stream.ts`)

6. **`toModelOutput` hook** — shapes what the model sees from a tool result. Prevents context explosion from large tool outputs. Must run inside message conversion. (`tool.ts:149`)

7. **`toolsContext` / DI channel** — typed context threaded into every `execute`, mutatable only via `prepareStep`. Clean channel for per-step memory/tenant/request state. (`generate-text.ts:309`)

8. **Schema repair one-shot retry** — catch JSON parse + type validation errors only, call user-supplied `repair_text(text, error)`, exactly one re-parse. (`parse-and-validate-object-result.ts:77`)

9. **Per-chunk timeout on streaming** — separate from step timeout, resets on each received chunk. Catches provider mid-stream stall before the full step timeout fires. (`stream-text.ts:1578`)

10. **Memory: index file + topic files** — `MEMORY.md` ≤200 lines always in context, topic files on-demand. No embeddings needed for basic agent memory. (v0 prompt lines 863-908)

11. **`taskNameActive`/`taskNameComplete`** — model controls its own progress UI labels via tool input schema. Maps to `message-metadata` stream part. (v0 Tools.json)

### Avoid

1. **Over-building the `Agent` class** — the AI SDK `ToolLoopAgent` is thin sugar (`prepareCall` + merged callbacks + 20-step default). Don't build a subclassed execution engine; keep the loop flat and the Agent as a frozen config bundle.

2. **Per-tool `needsApproval`** — deprecated in the AI SDK in favor of loop-level approval config. Don't implement it at the tool level; keep approval as a loop concern.

3. **Mode dispatch in `generateObject` (`auto`/`tool`/`json`)** — the SDK collapsed this to always use `responseFormat:{type:'json'}` and let each provider implement it. The historical `mode:'tool'` path (synthetic forced tool) is gone. Don't replicate the old 3-mode dispatch.

4. **Lazy tool loading as a complex system** — `ToolSearch` is a single-call schema loader (a meta-tool that returns JSON schemas). Don't over-engineer; it's a lookup function, not a service.

5. **v0's `automated_v0_instructions_reminder` as a first-class primitive** — it's a product-specific control plane injection. What's worth copying is the concept (operator instructions separate from user messages) not the implementation detail.

---

## 11. Byte-path implications (Lancelot-specific)

| Area | What the AI SDK / v0 does | What Lancelot already has | Gap / what to build |
|---|---|---|---|
| Outbound LLM call | `doGenerate`/`doStream` per `LanguageModelV2` | `TransportPool.request`/`stream` | Lancelot handles transport; **add the message-shape translation layer** (`LanguageModelV2` equivalent with `doGenerate`/`doStream` interface + ordered content parts) |
| Provider SSE → normalized parts | Per-provider `convert-to-X-messages.ts` + stream-part translation | Not built | Build per-provider adapters mapping native SSE to the 16-part union; `TransportPool` carries the bytes, the adapter normalizes them |
| Stream to browser | `toUIMessageStreamResponse()` → 27-chunk SSE | `@app.stream` R3 pump, SSE, TTFT metric | **Add UI Message Stream layer**: emit the 27-chunk protocol on top of the existing SSE pump; no new transport, just structured parts |
| Tool execution | `executeTools` with overlapped exec on `tool-input-end` | `@app.tool` + `lancelot.mcp.mount` | Add `on_input_end` hook to trigger overlapped execution; the byte pump already handles backpressure |
| Multi-step loop | `do…while` with `stopWhen` | Not built (agent loop is the build target) | Build the Python `StepLoop` with `stopWhen`/`prepareStep` — this is the core agent primitive |
| Context isolation between steps | `messagesForNextStep = [...prev, ...response]` threading | `lancelot.context` ContextVars | ContextVars track cost/tokens per step; the message-threading is a pure Python list-accumulation pattern, no Rust needed |
| Deferred tool results | `pendingDeferredToolCalls` map + loop-continue accounting | Not built | Needed for provider-executed tools (web search); simple map keyed by `toolCallId` |
| Per-chunk stream timeout | `chunkTimeoutMs` resetting on each chunk | `per-route timeout`, `header_read_timeout` | Add chunk-level timeout to the streaming path — fires if provider goes silent mid-stream |
| Memory | File-based index + topic files | Not built | Simple: `AgentMemory(index_path)` wrapping standard file I/O; no vector store for tier-0 |
| Tool progress UI | `taskNameActive`/`taskNameComplete` in tool input | `@app.tool` schemas | Optional fields in tool schema → emit as `message-metadata` part; zero-cost addition |
| Model string routing | `'openai:gpt-4o'` → provider lookup | Not built | `ProviderRegistry` with `:` separator; `resolveModel(str|Model)` — Python dict + string-split, trivial |
| Prompt cache stability | Provider `cacheControl` via `providerOptions` | `lancelot.canonical.dumps_canonical` | `dumps_canonical` handles message serialization; add `cache_control` passthrough in the `LanguageModelV2` call options shape |
| Cost tracking | Per-step usage accumulation | `lancelot.context.cost_usd`, `lancelot.upstream_metrics` | Cost already flows; wire it to `usage.totalTokens` in `StepResult` |
| TTFT metric | Not in AI SDK (transport-agnostic) | `lancelot.http.stream.ttft` | Already built — the Lancelot advantage over a pure TS stack |

---

## 12. Hype and depreciating items to ignore

- **v3/v4 spec versions in the AI SDK tree** — forward-churn. Build against v2 semantics; the ordered content list is the durable primitive regardless of version number.
- **v0's shadcn/ui/Next.js 16 specialization** — product-specific, not framework-primitive. The patterns (pre-populated scaffold, lazy tool loading, memory) are worth copying; the framework lock-in is not.
- **The specific 12-item v0 Marketplace enum** — commercial alignment, not architecture.
- **`smooth-stream` delay constants** — `10ms` per-word is a UX tunable, not a protocol primitive. Implement the word/line chunking transform; treat the delay as user-configurable.
- **AI SDK `generateImage`/`generateSpeech`/`generateVideo`/`rerank`** — each follows the same `doX` pattern; build on demand, not speculatively. The normalization principle is durable; the specific modalities are not all load-bearing for Lancelot day-1.
