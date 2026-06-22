# Mastra — RE Teardown Note (Lancelot lens)

> Source: `/Users/seventyleven/Desktop/researchfms/teardowns/MASTRA.md` (clone date 2026-05-30, `--depth=1` of `mastra-ai/mastra master`).
> All `file:line` cites refer to `packages/core/src/...` unless prefixed `rag:`.
> Labels: **KNOWN** = read in source at cited location; **INFERRED** = deduced from code shape.

---

## One-line thesis

**Mastra's agent loop is not a hand-written `while` loop — it is a `.dowhile` Workflow whose body is another Workflow (`.then → .foreach → .then…`). The same snapshot/suspend/resume machinery that powers user workflows powers HITL tool-approval pauses, durable agents, and `resumeStream()`.** Remove the workflow engine and the agent loop does not exist. That is the test for a rebuilt primitive vs. a wrapper, and Mastra passes it.

---

## 1. The durable-execution primitive (highest priority)

### 1a. Two-array compile: the durability trick

`Workflow` builder keeps two parallel arrays on every control-flow call (`workflows/workflow.ts:2100–2340`, KNOWN):

- `stepFlow: StepFlowEntry[]` — live graph (real `Step` objects + real predicate functions); used by the in-process engine.
- `serializedStepFlow: SerializedStepFlowEntry[]` — durable twin; every predicate stored as `fn.toString()`, every step reduced to `{id, description, metadata, component, serializedStepFlow, canSuspend}`. `canSuspend = Boolean(step.suspendSchema || step.resumeSchema)` precomputed at build time (`workflow.ts:2223`).

This dual push is why durability is "free": a different process can re-hydrate the graph topology from the serialized twin without needing the original closures. **The key constant:** `.map` serializes its function body truncated to **1000 chars** + `'...\n}'` if longer (`workflow.ts:1946-1948`).

### 1b. The 6 `StepFlowEntry` kinds (the complete type-dispatch surface)

(`default.ts:998-1014`, KNOWN):

```
{type:'step',        step}
{type:'sleep'|'sleepUntil', id, duration?|date?, fn?}
{type:'parallel',    steps:[{type:'step',step}...]}
{type:'conditional', steps:[...], conditions:[fn...], serializedConditions:[{id,fn:string}...]}
{type:'loop',        step, condition, loopType:'dowhile'|'dountil', serializedCondition}
{type:'foreach',     step, opts:{concurrency}}
```

### 1c. `.dowhile` — the agent loop is this

The handler (`handlers/control-flow.ts:559-790`, KNOWN):

```python
# pseudocode of the Rust-translatable logic
iteration = (prevStepResult.metadata.iterationCount ?? 1) - 1   # resume-aware cursor
do:
    if abortSignal.aborted: return canceled
    result = engine.executeStep(step, prevOutput=result.output,
                                iterationCount=iteration+1, resume=currentResume)
    currentResume = None if result.status != 'suspended' else currentResume
    if result.status != 'success': return result   # suspend/fail bubbles out immediately
    if abortSignal.aborted: return canceled
    isTrue = await condition(inputData=result.output)
    iteration++
while isTrue  # dountil: while NOT isTrue
```

Critical properties:
- **Body runs at least once** (do-while, not while-do) → agent always makes ≥1 LLM turn.
- **`iterationCount` is persisted in `stepResults[step.id].metadata`** — this is the resume cursor; a suspended loop resumes on the same iteration, not from zero.
- Non-`success` status (suspend/fail/cancel) exits the loop and bubbles up — HITL approval suspend inside the body pauses the whole `.dowhile`.

### 1d. `.foreach` with per-element suspend

`.foreach(step, {concurrency})` (`workflow.ts:2289-2340`, KNOWN). Previous step must return an array (compile-time guard). Default `concurrency: 1`. The handler replaced old `Promise.all`-on-slices with a `fastq` callback queue — "starts the next item as soon as any slot frees, keeping `concurrency` items running at all times instead of waiting for an entire batch" (verbatim comment `handlers/control-flow.ts:920-922`).

Resume cursor for foreach: `suspendPayload.__workflow_meta.foreachIndex` + `foreachOutput` (partial results array) + `resumeLabels` (`control-flow.ts:898, 911, 913-914`). A foreach that suspended on element 7 of 20 resumes there with elements 0–6 already persisted.

### 1e. `.parallel` — `Promise.all` fan-out, keyed merge

(`workflow.ts:2074-2135`, KNOWN). All branches start at once (no concurrency cap). Merge output is keyed by branch step-id: `{[stepId]: branchOutput}`. Short-circuit: any branch `failed` → whole step `failed`; any `suspended` → step `suspended` (single suspended branch suspends the entire parallel — no partial-completion of a parallel block). This is the key footgun.

### 1f. `.branch` — multi-way conditional (NOT first-match)

ALL conditions evaluated in parallel via `Promise.all`; every truthy branch executes (`control-flow.ts:259-359`, KNOWN). To get exclusive routing: write mutually-exclusive predicates. Used internally by the agent network for routing.

### 1g. `.waitForEvent` is REMOVED

**Correction (2026-05-30):** `.waitForEvent` throws `MastraError{id:'WORKFLOW_WAIT_FOR_EVENT_REMOVED'}` (`workflow.ts:1888-1903`, KNOWN). The replacement is the **suspend/resume** mechanism: step calls `suspend(payload)`, host later calls `run.resume({resumeData})`. Any note citing `.waitForEvent` as a live primitive is stale.

### 1h. Suspend/resume/time-travel/restart — the four re-entry modes

(`default.ts:687-766`, KNOWN). All three share one engine path via `startIdx`:

| Mode | `startIdx` source | Extra fields |
|---|---|---|
| **resume** | `resume.resumePath[0].shift()` | `resumePayload`, `forEachIndex`, `label` |
| **timeTravel** | `timeTravel.executionPath[0].shift()` | past `stepResults`, `state` |
| **restart** | `restart.activePaths[0].shift()` | prior `stepResults`, `stepExecutionPath` |
| **fresh** | 0 | `{input}` seeded |

`resumeStream()` rehydrates a suspended agent: at loop entry (`loop.ts:119-122`, KNOWN), if `resumeContext.snapshot` exists, scan `snapshot.context[*]` for a step with `status==='suspended'` and `suspendPayload.__streamState` — that `__streamState` becomes `initialStreamState` injected into a fresh `MastraModelOutput`.

### 1i. The snapshot schema (KNOWN columns)

One `jsonb` blob in `mastra_workflow_snapshot(workflow_name, run_id, resourceId, snapshot, createdAt, updatedAt)` (`storage/constants.ts:517-576`). The blob carries: `serializedStepFlow`, `stepResults`, per-step `status + suspendPayload`, `resumePath`, serialized `state`, `__streamState`.

The four storage tables:

| Table | Key columns |
|---|---|
| `mastra_workflow_snapshot` | `workflow_name`, `run_id`, `snapshot jsonb` |
| `mastra_threads` | `id`, `resourceId`, `title`, `metadata jsonb` |
| `mastra_messages` | `id`, `thread_id`, `content`, `role`, `type`, `resourceId` |
| `mastra_resources` | `id`, **`workingMemory text`**, `metadata jsonb` |

Resource-scoped working memory lives in `mastra_resources.workingMemory` — a single text column — which is why it persists across all threads.

### 1j. Run-status lifecycle (10-state enum, verbatim)

(`workflows/types.ts:264-274`, KNOWN):
```
'running' | 'success' | 'failed' | 'tripwire' | 'suspended' |
'waiting' | 'pending' | 'canceled' | 'bailed' | 'paused'
```
Fresh run is `'pending'`. Persist snapshots only on `pending|paused|suspended` (agentic-loop) or `suspended` (exec workflow) — **never** `'running'`, to avoid clobbering an existing suspended snapshot (`agentic-loop/index.ts:66`, `agentic-execution/index.ts:83`, KNOWN).

---

## 2. The agent loop topology (KNOWN, cited)

### 2a. Outer loop — `.dowhile`

`loop/workflows/agentic-loop/index.ts:56` (KNOWN):
```
createWorkflow({id:'agentic-loop', ...})
  .dowhile(agenticExecutionWorkflow, stopPredicate)
  .commit()
```

The `stopPredicate` (`index.ts:80-278`, KNOWN condensed):
1. Drain pending external signals (cross-process mid-run injections).
2. Build `currentStep = {content, toolCalls, toolResults, usage, finishReason, ...}` from delta slice.
3. Evaluate `stopWhen` conditions: `conditions = await Promise.all(stopWhenArray.map(c=>c({steps: accumulatedSteps})))`.
4. Run `onIterationComplete` hook (may force continue/stop).
5. Emit `step-finish` chunk.
6. Return `inputData.stepResult.isContinued` (false = stop the loop).

`maxSteps` is enforced by `accumulatedSteps.length < rest.maxSteps` checks in the feedback path (`index.ts:219,229`).

### 2b. Inner turn — `agenticExecutionWorkflow`

`loop/workflows/agentic-execution/index.ts:73` (KNOWN):
```
createWorkflow({id:'executionWorkflow', ...})
  .then(llmExecutionStep)          // one LLM call (stream), emit text/reasoning/tool-call chunks
  .map(toolCalls => ...)           // extract toolCalls array from output
  .foreach(toolCallStep, {concurrency}) // per tool: resolve → approve/suspend → execute
  .then(llmMappingStep)            // fold tool results back into messages/output
  .then(backgroundTaskCheckStep)   // poll/await background tasks
  .then(signalDrainStep)           // absorb external signals
  .then(isTaskCompleteStep)        // set stepResult.isContinued + reason
  .commit()
```

### 2c. Tool approval — suspend/resume mechanism (KNOWN, exact path)

`tool-call-step.ts:432,546,593`:
1. `requireToolApproval = requestContext.get('__mastra_requireToolApproval')` (flag key is the literal string).
2. `toolRequiresApproval = requireToolApproval || tool.requireApproval`.
3. If approval required and not yet granted: emit `{type:'tool-call-approval', payload:{toolCallId, toolName, args}}` then `suspend({requireToolApproval:{...}, type:'approval'})`.
4. Workflow goes `suspended`; snapshot persists. Host later calls `resumeStream({resumeData:{approved:true}})`.
5. Tool-internal suspend (`tool-call-step.ts:546,593`): tool's own `execute` calls `suspend(payload)` → `{type:'tool-call-suspended'}` chunk.

**`addToolMetadata` (`tool-call-step.ts:128,173`)**: writes `pendingToolApprovals` and `suspendedTools` (keyed by toolName, values: `{toolCallId, toolName, args, type, runId, suspendPayload?, resumeSchema}`) into the last assistant message's `content.metadata` — so the pause survives a page refresh.

---

## 3. Memory subsystem — exact constants and algorithms

### 3a. Constants table (KNOWN, runtime values beat doc values)

| Constant | Value | Source |
|---|---|---|
| `lastMessages` default | 10 | `memory/memory.ts:84` |
| `semanticRecall` default | `false` | `memory/memory.ts:85` |
| Semantic recall `topK` **runtime** | **4** | `processors/memory/semantic-recall.ts:14` |
| Semantic recall `messageRange` **runtime** | **1** | `processors/memory/semantic-recall.ts:15` |
| Semantic recall `scope` | `'thread' | 'resource'` | `memory/types.ts:846` |
| `workingMemory.enabled` default | `false` | `memory/memory.ts:86` |
| Working memory scope | `'resource'` (default — spans conversations) | `working-memory.ts:101` |
| Embedding cache | LRU `max:1000`, key=`xxhash h64("<indexName>:<content>").toString(16)` | `embedding-cache.ts:434-437` |
| Distance metric | cosine | `semantic-recall.ts:505-508` |

Doc comment cites `topK:3` / `messageRange:2`; the **runtime constants** are `topK=4` / `messageRange=1`. The runtime wins.

### 3b. Semantic recall algorithm (KNOWN, exact steps)

`processors/memory/semantic-recall.ts:370-476`:

1. Embed query via `embedMessageContent`; check global LRU cache first (`xxhash h64` key).
2. `vector.createIndex({indexName, dimension, metric:'cosine'})` — idempotent.
3. `vector.query({indexName, queryVector, topK:4, filter: scope==='resource' ? {resource_id} : {thread_id}})`. Scope is a **vector metadata filter**, not a post-filter.
4. Drop hits with `score < threshold` if `this.threshold` set.
5. Expand each hit by `±messageRange=1` neighbor messages via `storage.listMessages({include:[{id, withNextMessages:1, withPreviousMessages:1}], perPage:0})`.
6. Stable-sort recalled messages (`sortMessagesForRecall`) before injection (prevents non-deterministic reordering on score ties).
7. Inject as `'memory'` role into `MessageList`.

### 3c. Working memory write mechanism (KNOWN)

The agent writes by calling the auto-registered `updateWorkingMemory` tool which does a **full template rewrite** (pass the entire Markdown/JSON string back). VNext: `__experimental_updateWorkingMemoryVNext({searchString?, ...})` supports find-and-replace of a targeted region (`memory/memory.ts:677-689`).

Working memory lives on `mastra_resources.workingMemory` (resource row), not the thread. Guard: missing storage adapter throws `MastraError{id:'WORKING_MEMORY_MISSING_STORAGE_ADAPTER'}`.

The `MessageHistory` processor strips `updateWorkingMemory` invocations and `<working_memory>` tags from history before sending to the model (`processors/memory/working-memory.ts:168-198`).

### 3d. Working memory system prompt (KNOWN verbatim, VNext variant)

The key guideline that prevents unnecessary tool calls (guideline 5) and explains cross-conversation persistence (guideline 6):

```
5. If your memory has not changed, you do not need to call the updateWorkingMemory tool.
   By default it will persist and be available for you in future interactions.
6. Information not being relevant to the current conversation is not a valid reason to
   replace or remove working memory information. Your working memory spans across multiple
   conversations and may be needed again later, even if it's not currently relevant.
```

---

## 4. RAG rerank math (KNOWN, exact weights)

`packages/rag/src/rerank/index.ts:9-14`:
```
Default weights: {semantic: 0.4, vector: 0.4, position: 0.2}
```
Weights validated to sum to exactly 1 via `big.js` (`rerank/index.ts:118-124`). Per candidate `i` of `N`:
```
semanticScore = scorer.getRelevanceScore(query, candidate.metadata.text)  # LLM/Cohere, 0..1
vectorScore   = candidate.score                                            # cosine similarity
positionScore = 1 - i / N                                                  # earlier = higher
finalScore    = 0.4*semanticScore + 0.4*vectorScore + 0.2*positionScore
```
Optional query-magnitude boost: if L2 magnitude > 10 → multiply by 1.1; if > 5 → by 1.05 (`rerank/index.ts:81-88, 149-150`). Top-k after fusion defaults to **3** (`rerank/index.ts:101`).

Chunk defaults (`document/transformers/text.ts:15-32`, KNOWN): `maxSize=4000`, `overlap=200`, separator `'\n\n'` (`transformers/character.ts:55`), `stripWhitespace=true`. Guard: `overlap > maxSize` throws.

---

## 5. Processors — the guardrail pipeline

`ProcessorRunner` (`processors/runner.ts:273`): per-chunk stream transform. A `TripWire` thrown by any processor (`agent/trip-wire.ts`) is caught and emits `{type:'tripwire', payload:{reason, retry, processorId}}` and stops the stream. `retry` re-enters the loop for that turn.

Key built-in processors and their exact constants (KNOWN):

| Processor | Key constant |
|---|---|
| `CostGuardProcessor` (`cost-guard.ts`) | `maxCost` (USD); time window **7 days** (`:117`); scope `'run'|'resource'` default `'resource'`; `strategy: 'block'|'warn'` |
| `ModerationProcessor` | threshold **0.5** (`:52`) |
| `PromptInjectionDetector` | threshold **0.7** (`:48`); strategy `'block'|'warn'|'filter'|'rewrite'` |
| `TokenLimiterProcessor` | `countMode: 'cumulative'|'per-step'` default cumulative; `strategy: 'truncate'|'abort'` |
| `ToolSearchProcessor` | semantic match of tools to query (avoids dumping 100 tools into every call) |
| `ResponseCacheProcessor` | keyed on input; returns cached completion |
| `UnicodeNormalizer` | NFKC-style normalization before the model sees text (homoglyph/spoofing defense) |
| `BatchPartsProcessor` | coalesces text-deltas to reduce chunk count |

**A whole Workflow can be a processor** (`ProcessorRunner:327` `executeWorkflowAsProcessor`) — a guardrail can itself be a multi-step agentic pipeline.

---

## 6. Streaming protocol (86 chunk types)

`stream/types.ts` defines 86 distinct `type:` literals, each `{type, runId, from: ChunkFrom, payload}`. Lifecycle (KNOWN):

- Run: `start → step-start → ... → step-finish → finish`. Abort: `abort`, `error`, `tripwire`.
- Text: `text-start`, `text-delta`, `text-end`.
- Tools: `tool-call-input-streaming-start`, `tool-call-delta`, `tool-call-input-streaming-end`, `tool-call`, `tool-result`, `tool-error`.
- HITL: `tool-call-approval`, `tool-call-suspended`, `tool-execution-approval/-suspended/-abort/-end`, `tool-output`.
- Background tasks: `background-task-started/-running/-progress/-output/-completed/-failed/-cancelled/-suspended/-resumed`.
- Multi-agent: `routing-agent-start/-end`, `network-execution-event-step-finish/-finish`, `agent-execution-start/-end/-abort/-approval/-suspended`.

**`saveQueueManager`** (`loop.ts:64`) batches message persistence so DB writes don't block the stream.

---

## 7. DI container — `RequestContext`

`di/index.ts` / `request-context/request-context.ts` (KNOWN). Typed key→value map scoped to one request, threaded through the entire loop and into every tool's `execute`. Methods: `get(key)`, `set(key, value)`, `has`, `delete`, `clone()` (for sub-runs). Per-key type inference (test-d file: `get('age')` returns `number`, not a union). Well-known internal keys: `MASTRA_RESOURCE_ID_KEY`, `MASTRA_THREAD_ID_KEY`, `MASTRA_VERSIONS_KEY`, `__mastra_requireToolApproval`.

---

## 8. Scorers / evals pipeline

`evals/base.ts:280` (KNOWN). Four-step builder:
```
createScorer({id, type, description})
  .preprocess((run) => ...)        # derive intermediate data
  .analyze((run) => ...)           # produce analysis (can call an LLM via promptObject)
  .generateScore((run) => number)  # REQUIRED numeric score
  .generateReason((run) => string) # optional justification (gets the computed score in ctx)
```

Each step can be a plain function or a `PromptObject` (LLM-backed step). Results accumulate via `AccumulatedResults` so later steps see earlier outputs. Score attached to OTel trace if `mastra.observability.addScore` available (`base.ts:620`). Scorer must have an `id` or throws `MASTR_SCORER_FAILED_TO_CREATE_MISSING_ID`. Scorers are first-class persisted objects runnable against live traces (`evals/scoreTraces/`).

---

## Lancelot byte-path implications

### Already have — no action

| Mastra component | Lancelot equivalent |
|---|---|
| `TransportPool.request()` w/ traceparent + idempotency | Full match — Lancelot already injects request-id, agent-id, traceparent |
| `lancelot.upstream_metrics` provider/token detection | Full match — Mastra relies on AI SDK for this; Lancelot builds its own |
| `lancelot.context` cost_usd/tokens_in/out/agent_id | Direct equivalent of Mastra's per-request cost accumulation |
| `@app.batch` request coalescing | Mastra has nothing comparable — Lancelot is strictly ahead here |
| `@app.stream` + R3 pump + TTFT metric | Lancelot stream is faster (GIL-released pump); Mastra's stream is `ReadableStream` in TS |
| `lancelot.mcp.mount` | Mastra has `mcp/` wrapper; Lancelot already has mount + minimal-return discipline |
| `lancelot.canonical.dumps_canonical` | Mastra has no prompt-cache stability primitive — Lancelot is ahead |
| `lancelot.memory_pool.MemoryPool` | NOTE: this is byte-memory OOM, not agent memory — different concept from Mastra's MessageHistory/WorkingMemory |

### Gaps the Lancelot agent layer needs (maps from Mastra)

| What Mastra has | What Lancelot needs | Mechanism to copy |
|---|---|---|
| `.dowhile` workflow engine with dual-array compile | **Durable execution primitive** — the single most critical gap. The agent loop IS this. | Dual-array builder: `StepFlow + SerializedStepFlow`, `fn.toString()` for predicates, `canSuspend` precomputed. Handler: do-while with `iterationCount` in `stepResults[id].metadata` as resume cursor. |
| `suspend(payload)` + `run.resume({resumeData})` | **HITL / durable suspend/resume** | `suspendSchema`/`resumeSchema` Zod fields on each step; persist snapshot as JSONB blob; `resumePath[0].shift()` to reenter. |
| `foreach` w/ per-element suspend cursor | **Parallel tool dispatch w/ resume** | `foreachIndex` in `suspendPayload.__workflow_meta`; `fastq`-style bounded queue not `Promise.all` batches. |
| `RequestContext` typed DI | **Request-scoped DI** | Already partially covered by `lancelot.context` ContextVars, but Mastra's typed `clone()` for sub-runs is worth copying. |
| `MessageHistory` processor stripping memory plumbing | **Context assembly + compaction** | Strip working-memory tool calls and `<working_memory>` tags from history before the model call. |
| Working memory: `updateWorkingMemory` tool + VNext patch | **Persistent working memory** | Full-template rewrite (simple); VNext find-and-replace for efficiency. Store on `resource` row (spans threads), not thread. |
| Semantic recall: xxhash LRU cache + cosine topK=4 + ±1 expansion + scope filter | **Semantic memory recall** | Exact algorithm: LRU(1000) keyed by `h64("<index>:<content>")`, cosine topK=4, vector metadata scope filter, ±1 expansion, stable-sort before injection. |
| `saveQueueManager` — batched async DB writes | **Non-blocking persistence** | Decouple message DB writes from the stream; batch them without blocking chunks. |
| `CostGuardProcessor` w/ 7-day window, `'resource'` scope | **Cost guardrail** | Mastra's `lancelot.context.cost_usd` already accumulates; need the TripWire abort protocol + configurable window/scope. |
| `ToolSearchProcessor` — semantic tool selection | **Dynamic tool selection** | Embed the user query, cosine match against tool descriptions, inject only top-K tools per turn. Avoids the 100-tool-dump anti-pattern. |
| `UnicodeNormalizer` + `PromptInjectionDetector` | **Input sanitization processors** | NFKC normalization (homoglyph defense); LLM-based injection detector w/ threshold=0.7 and `'rewrite'` strategy. |
| `MastraScorer` 4-step builder | **Eval DSL** | `.preprocess → .analyze → .generateScore → .generateReason` pipeline with `PromptObject` (LLM-backed) steps and score attached to trace. |
| Weighted rerank: `0.4*semantic + 0.4*vector + 0.2*position` | **RAG rerank** | Validate weights sum to 1; magnitude boost (>10 → ×1.1, >5 → ×1.05); topK=3 after fusion. |
| Chunk defaults: `maxSize=4000`, `overlap=200`, sep `'\n\n'` | **RAG chunking** | Guard: `overlap > maxSize` throws. |

---

## Anti-patterns to avoid

1. **Hand-written `while` agent loop** — the durable/HITL story falls apart immediately. The loop MUST be the workflow engine's `.dowhile`; that is the architectural bet worth copying.
2. **`Promise.all`-on-slices for `foreach`** — the old pattern. Use a bounded queue (`fastq`-style) so slots free as items complete, not at batch boundaries.
3. **Full-snapshot diffs for watch events** — Mastra deprecated v1 (whole-snapshot diff) in favor of v2 (per-step events via pub/sub topic `workflow.events.v2.<runId>`). Start with v2.
4. **Persisting `'running'` status snapshots** — clobbers an existing `suspended` snapshot. Persist only on `pending|paused|suspended`.
5. **`.waitForEvent` as a primitive** — removed in favor of suspend/resume. Do not build it.
6. **Working memory on the thread row** — should be on the resource row so it spans conversations.
7. **Doc-comment constants over runtime constants** — the teardown caught `topK` (doc:3, runtime:4) and `messageRange` (doc:2, runtime:1). Always cite the runtime source.
8. **Dumping all tools every turn** — the `ToolSearchProcessor` pattern (semantic selection per query) is the production answer at scale.

---

## Hype / depreciating items (skip)

- **Durable agent via Inngest/evented engine** — the evented engine (`workflows/evented/`) is a Pregel-ish Inngest-specific path. The in-process engine with a Postgres snapshot store covers 95% of use cases and is much simpler. Inngest coupling is tactical.
- **A2A (Agent-to-Agent protocol)** — the `a2a/` directory implements cross-framework agent calls; this is an early spec (2025-era), churn is high.
- **`openai-websocket` chunk type** — provider-specific realtime; churns with OpenAI API changes.
- **Provider history compat rewrites** — `provider-history-compat.ts` rewrites Anthropic vs OpenAI tool-call formats. Lancelot's TransportPool abstracts the transport layer; the message-shape translation problem is real but should be done at the model adapter level, not as a per-chunk stream transform.
