# Evals & Observability — Mechanism-Level Distillation

**Sources mined:** LAMINAR.md (Laminar OSS + recovered private Signals, git `de18b530`), WEIGHTS_AND_BIASES.md (W&B + Weave), ADALINE.md (Adaline eval engine + observability schema), CODERABBIT.md (CodeRabbit context-engineering + eval loop).

**Focus (per task brief):** eval-as-run, judge calibration, pass^k + per-step rate, trace-based process evals, OTel gen_ai.* spans, incident→regression in one line.

---

## 1. Eval-as-run: The Three Patterns Worth Copying

### 1.1 Laminar Signals: filter → summarize → agent (THE canonical pattern)

Laminar's Signals system is the most fully-realized eval-as-run implementation in the corpus. Every claim is KNOWN from git history at `de18b530` (recovered before the 36-minute OSS window closed).

**The pipeline (`common.rs::process_run`):**
1. **Filter pass** — a separate LLM call (Claude Opus 4.6 via Bedrock, hardcoded, temperature=1.0, max_tokens=4096, thinking=Medium+with-thoughts) reads the raw trace and generates **glob-pattern drop rules** that strip noisy spans before the eval agent sees them. Goal: compress 50–200k-token raw trace to 5–20k tokens. Cache key: `span_drop_rules:{project_id}:{signal_id}:{sig_hash8}:{fingerprint8}`, TTL=7 days. Skip filter-gen entirely if `trace_string.len() > 1_000_000`. Rules without `name` or `path` matcher are discarded (safety valve). Spans with exception events bypass all drop rules (errors are sacred). (`LAMINAR.md §6.6 / §19.§3.5`)
2. **Summarize pass** — extracts every unique LLM system prompt from the trace, replaces them with `system_prompt: sp_XXXX` references, and runs a single LLM call to compress each into a signal-relevant summary. Cache key uses `structural_skeleton_hash` (strips numbers/IDs/timestamps/stopwords — prompts differing only in runtime params share a cache entry). Cache TTL=30 days. The filter-pass fingerprint is derived from the main agent's compressed summary. (`LAMINAR.md §6.7 / §19.§3.4`)
3. **Agent loop** (max 5 steps, env-overridable: `SIGNAL_JOB_MAX_ALLOWED_STEPS`) — receives the compressed trace and has exactly three tools: `search_in_spans`, `get_full_spans`, `submit_identification`. The agent MUST call a function each turn (plain text = invalid). `search_in_spans` does Rust-side fuzzy search (NOT ClickHouse FTS) — two-pass: normalized substring match + word-proximity sliding window (`max_window_bytes = total_query_bytes*5 + 200`, `CONTEXT_PADDING=256`, `MAX_MATCHES=5`). (`LAMINAR.md §6.5 / §19.§4, §19.§9`)

**Compression constants (all KNOWN from source):**
```
TRUNCATE_THRESHOLD = 2048        # chars; single-field truncation floor
LLM_MESSAGE_MAX_CHARS = 3000     # per-string cap inside LLM input array
LLM_INPUT_TOTAL_MAX_CHARS = 8192 # hard cap on total serialized LLM input
TOOL_DEDUP_OVERLAP_THRESHOLD = 0.75  # Jaccard-like; tool input that mirrors LLM output → replaced with <from_llm_output> ref
MAX_ALLOWED_STEPS = 5
FILTER_CACHE_TTL = 7 days
SUMMARY_CACHE_TTL = 30 days
SIGNAL_TRIGGER_LOCK_TTL = 3600s # distributed dedup lock per (project,signal,trace)
```
(`LAMINAR.md §19.§5`)

**The signal definition is user-authored:** a natural-language prompt + JSON output schema. The agent fills the schema when `identified=true`, with `severity ∈ {critical, warning, info}`. This is NOT a fixed evaluator type — it is a user-programmable assertion over any trace shape. (`LAMINAR.md §6.1`)

**Default signal Laminar ships:**
```
name: "Failure Detector"
prompt: "Analyze this trace for concrete issues: tool call failures, API errors,
         loops or repeated calls, wrong tool selection, logic errors, and abnormally
         slow or expensive spans. Only report problems visible in the trace data."
schema: { description: string, category: enum[tool_error, api_error, logic_error, looping, wrong_tool, timeout, other] }
trigger: total_token_count > 1000 AND root_span_finished == true
```
(`LAMINAR.md §6.1`)

**Eval-as-run implication:** each Signals run fires in the production trace-processing path (`check_and_push_signals` called from `process_span_messages` at the end of each batch). This is eval-as-run, not eval-as-batch.

**The two-turn prompt structure is the key:** SYSTEM_PROMPT (data conventions + field tags + tool rules + span reference format + `{{fullTraceData}}`) is the first turn; IDENTIFICATION_PROMPT (`{{developer_prompt}}`) is the second. The second turn brings in the user-defined assertion. This separation lets the trace-awareness instructions be stable while the assertion is fully user-defined. (`LAMINAR.md §19.§3.1-3.2`)

### 1.2 W&B Weave: eval-as-traced-call-tree

`weave.Evaluation(dataset, scorers, trials=1)` produces a complete call tree (eval → per-row predict → per-scorer) stored in ClickHouse. Each model prediction and each scorer is itself a traced `@weave.op`, so the eval is queryable as a span tree with auto-computed `summary{usage, costs}`. (`WEIGHTS_AND_BIASES.md §8.4`)

**Concurrency:** `predict_and_score` runs all scorers via `asyncio.gather(*scorer_tasks)` — scorers run concurrently per row. `get_eval_results` chains `repeat(rows, trials)` and runs `predict_and_score` over all rows concurrently. This is how you batch-eval at scale with no extra orchestration. (`WEIGHTS_AND_BIASES.md §8.4`)

**Sampling:** child traces are never independently sampled. If a root call is sampled, its entire subtree is kept (`_should_sample_traces` returns False if there's a current call). This ensures you get complete eval trees, not partial ones. (`WEIGHTS_AND_BIASES.md §8.1`)

### 1.3 Adaline: 6-type evaluator taxonomy + continuous eval on live spans

The Adaline evaluator types (all KNOWN from OpenAPI spec):
1. `llm-as-a-judge` — judge prompt string; runs LLM on row output; costs tokens
2. `response-length` — `{value, unit: words|characters|tokens, operator: less|greater|equals}`
3. `latency` — `{value, unit: ms|s, operator}`
4. `cost` — `{value, unit: USD, operator}`
5. `javascript` — raw JS code run server-side (sandboxed) against the row
6. `text-matcher` — 7 operators: `regex`, `equals`, `starts-with`, `ends-with`, `contains-all`, `contains-any`, `not-contains-any`

**Eval lifecycle:** `POST /prompts/{id}/evaluations` returns **202 Accepted** (async job). 6-state machine: `queued → running → completed | failed | cancelled` (with transient `cancelling` for cooperative drain — NOT hard kill). Per-row `EvaluationResult` has `grade ∈ {pass, fail, unknown}`, `score`, `reason`, and `failedRows` separate from `completedRows`. (`ADALINE.md §2.3-2.4`)

**Continuous eval on production spans:** `runEvaluation: bool` on `POST /logs/span` — when `true`, forces evaluation on this specific span regardless of sample rate. This wires evaluators as online production monitors, not just offline test gates. (`ADALINE.md §4.6`)

**The `EvaluationRunTimeline` timestamp trap:** `submittedAt` is Unix ms, but `startedAt`/`finishedAt` are bare integers in *seconds* (not ms). Documented inconsistency in the schema. A consumer must handle both. (`ADALINE.md §2.3`)

---

## 2. Judge Calibration

### 2.1 The 6× token reduction result (CodeRabbit — hard numbers)

CodeRabbit measured three context strategies for outside-diff review (INFERRED from founder talk, `core/TRANSCRIPTS_DEEPLEARNINGAI.md:2761-2787`):

| Strategy | Tokens | Time | Found the bug? |
|---|---|---|---|
| Diff-only | 17,000 | 10s | No |
| Brute-force (all connected files, JSON) | 110,000 | 38s | Yes |
| **Optimized slicing (direct callers/callees only, XML tags)** | **18,300** | faster | **Yes** |

The mechanism: (1) relevance filtering — only include files with a direct contract relationship (callers+callees of modified symbols, not transitive closure); (2) format efficiency — XML/tag envelopes beat JSON for three reasons: training distribution (LLMs see more tagged HTML/XML than JSON), token efficiency (JSON structural chars `{}"":,` accumulate), and long-context robustness (human-legible tags act as navigation anchors at 100K+ tokens). (`CODERABBIT.md §3.3-3.4`)

**The judge calibration lesson:** an LLM judge reading a brute-force context (110K tokens, 38s) and one reading a relevance-filtered context (18K tokens) produce the SAME result on this benchmark — the extra 92K tokens were noise that added latency and cost but no signal. Calibrating the context window IS calibrating the judge.

### 2.2 Field tags as judge calibration (Laminar)

The Laminar system prompt teaches the model exactly when NOT to use tools by tagging each field:
- `[complete]` — fully visible; do NOT search or fetch
- `[truncated]` — partial; retrievable
- `<omitted N chars>` — excluded; retrievable
- `<empty>` — no data; do NOT search
- `<from_llm_output span_id='...'>` — args are in the referenced span; do NOT search

**Result:** "A single unnecessary search can double the total cost of the analysis." The field-tag system is how Laminar constrains the judge to never waste a tool call on data that's already visible. This is judge calibration by context discipline, not by post-hoc scoring. (`LAMINAR.md §19.§3.1`)

### 2.3 Light/heavy model split for calibration (CodeRabbit + predecessor)

The OSS predecessor `ai-pr-reviewer` pins the load-bearing numbers:
- Light model: `gpt-3.5-turbo`, temperature=`0.05`, for per-file summarize + triage
- Heavy model: `gpt-4`, for per-hunk line-by-line review
- Timeouts: light 360,000ms (6 min); concurrency: `openai_concurrency_limit = 6`, `github_concurrency_limit = 6` (separate `p-limit` instances)
- Batch size for summary: `batchSize = 10` files per heavy-model call
- Max files: `max_files = 150`

The triage step parses `/\[TRIAGE\]:\s*(NEEDS_REVIEW|APPROVED)/` — only `NEEDS_REVIEW` files hit the heavy model. This is ~50% token savings on typical PRs. (`CODERABBIT.md §5.1-5.2, §5.6`)

**The durable pattern:** cheap model for breadth (triage/summarize), expensive model for depth (per-hunk reasoning over assembled context). This is calibrated judge routing, not arbitrary model selection.

---

## 3. pass^k and per-step rate

### 3.1 Laminar: the step budget is the reliability proxy

`MAX_ALLOWED_STEPS = 5` bounds per-eval LLM calls. The filter pass + summarize pass each add 1 LLM call before the agent loop starts, so a max-cost eval is 7 LLM calls. At Laminar's Pro pricing ($0.005/step), max cost per eval = $0.035. (`LAMINAR.md §14`)

**Per-step rate is tracked directly:** Laminar self-traces every signal run via `emit_internal_span` to an `internal_project_id` — each LLM call, tool call, and filter/summarize pass emits a span with `model`, `provider`, `input_tokens`, `output_tokens`, `input_cached_tokens`. This gives a per-step distribution across all evals in production. (`LAMINAR.md §7`)

**pass^k mechanism:** `SignalMode.Batch = 0 / Realtime = 1`. Batch mode uses Gemini Batch API or Bedrock async and costs less (2× cheaper per the docs). Realtime fires synchronously. The DLX-backed waiting queue implements "wait then poll" without a scheduler: `SIGNAL_JOB_WAITING_BATCH_QUEUE` uses `x-dead-letter-exchange = SIGNAL_JOB_PENDING_BATCH_EXCHANGE`; message TTL default `300_000ms` (5 min) → reroutes to pending poller. (`LAMINAR.md §6.3`)

**Retry policy (`pendings_consumer.rs`):** `DEFAULT_RETRY_COUNT = 4` (env: `SIGNALS_MAX_RETRY_COUNT`). Backoff on the realtime path (`realtime_api.rs`): initial=1s, multiplier=1.5, max_interval=10s, max_elapsed=60s. (`LAMINAR.md §19.§5.6, §5.9`)

### 3.2 W&B Weave: `trials` parameter for pass^k

`Evaluation(dataset=..., scorers=[...], trials=1)` — the `trials` parameter repeats the dataset N times (`chain.from_iterable(repeat(rows, trials))`), giving pass@k when k=trials. The results are all concurrently gathered across rows. (`WEIGHTS_AND_BIASES.md §8.4`)

**Tracing sample rate:** `tracing_sample_rate` on `@weave.op` gates which root calls are traced. Once a root call is sampled, its entire subtree is kept (children are never independently sampled). This means pass^k for evals that ARE traced is complete; for unsampled runs you have no trace. (`WEIGHTS_AND_BIASES.md §8.1`)

---

## 4. Trace-based Process Evals (OTel gen_ai.* + Span Tree)

### 4.1 Laminar's canonical OTel attribute normalization (THE reference implementation)

Laminar normalizes 6+ instrumentation libraries into one canonical model. This is the most complete gen_ai.* implementation in the corpus. Key providers and their span-type inference:

**Span type inference rules (KNOWN from `spans.rs`, `provider/mod.rs`):**
- `gen_ai.operation.name ∈ {chat, text_completion, embeddings, generate_content}` → `LLM`
- `gen_ai.operation.name == execute_tool` → `Tool`
- `gen_ai.operation.name == invoke_agent` → `Default`
- pydantic_ai fallback: any span with `gen_ai.tool.call.{arguments|result}` → `Tool` (even if operation name is absent)

**LangChain ≤ 0.43.0 bug:** `provider="langchain"` instead of the actual model provider — Laminar special-cases this. (`LAMINAR.md §4`)

**Vercel AI SDK normalization (`ai.model.id`, `ai.model.provider`, `ai.usage.{inputTokens,outputTokens,cachedInputTokens,inputTokenDetails.cacheReadTokens}`)** — the `cachedInputTokens` and `inputTokenDetails.cacheReadTokens` fields give cache-hit counts. (`LAMINAR.md §4`)

**OTel attribute preservation rule:** "Backend preserves the native OTel GenAI message shape end-to-end — `gen_ai.input.messages`/`gen_ai.output.messages` are deserialised from JSON string to `Value` but NOT reshaped into Laminar's `ChatMessage` struct." Lossless preservation means downstream process evals see the original provider payload. `gen_ai.system_instructions` is prepended to input messages as a synthetic `{role: "system", parts: [...]}` entry. (`LAMINAR.md §4`, CLAUDE.md citation)

**Laminar's internal attributes:**
- `lmnr.internal.tracing_level ∈ {off, meta_only}` — strip content for sensitive data
- `lmnr.association.properties.{session_id,user_id,trace_type,metadata.*}` — correlate traces to users/sessions

### 4.2 W&B Weave: span tree + streaming accumulator

**The span tree (`trace/op.py`):**
```python
parent_call = call_context.get_current_call()  # ContextVar for span tree linkage
return client.create_call(func, inputs, parent_call, ...)  # assigns trace_id, parent_id
```
This is OTel-identical span parenting via ContextVar — no explicit context passing required. (`WEIGHTS_AND_BIASES.md §8.1`)

**Streaming accumulator (the detail every homebrew tracer misses):** for streamed OpenAI/Anthropic responses, the `openai_accumulator` folds SSE delta chunks back into a single logical output and reconstructs `usage` (which streamed responses omit until the final chunk). Result: a streamed call traces as ONE span with full I/O and token counts. Without this, your process eval sees incomplete input/output on streamed calls. (`WEIGHTS_AND_BIASES.md §8.2`)

**Auto-patch list (KNOWN from `integrations/__init__.py`):**
`openai, anthropic, mistral, litellm, cohere, google_genai, vertexai, groq, langchain, llamaindex, dspy, instructor, crewai, autogen, smolagents, openai_agents, openai_realtime, huggingface, nvidia, notdiamond, verdict`

**Batching (`async_batch_processor.py`):**
- `DEFAULT_MAX_BATCH_SIZE = 100`
- `DEFAULT_MIN_BATCH_INTERVAL = 1.0s`
- `DEFAULT_MAX_QUEUE_SIZE = 10_000`
- `REMOTE_REQUEST_BYTES_LIMIT = (32-1)*1024*1024` (31 MiB)
- On persistent failure: **log-and-drop** (never blocks or crashes the app)
- Spans assigned `id`/`trace_id` locally — span tree exists before any network round-trip

(`WEIGHTS_AND_BIASES.md §17`)

### 4.3 Adaline: typed span taxonomy for process evals

Adaline's `LogSpanContent` discriminated union (all KNOWN from OpenAPI):

| `type` | What it captures for process evals |
|---|---|
| `Model` | one LLM call: provider, model, input/output JSON, variable values, cost override |
| `ModelStream` | streamed LLM: raw token stream (`output:string`) + assembled final (`aggregateOutput` JSON) — both preserved |
| `Tool` | external tool/API call the agent made |
| `Guardrail` | safety/compliance check (PII, moderation, policy) |
| `Retrieval` | RAG query + retrieved docs |
| `Function` | custom application logic step |
| `Embeddings` | vector-embedding call |

**The `parentReferenceId` span-tree builder:** each span names its parent's external id, allowing arbitrary span trees keyed by YOUR own ids. (`ADALINE.md §4.4`)

**`promptId` on a span** links a production call back to the registered prompt in Adaline — production traffic appears in that prompt's monitor and can be evaluated against its evaluators. This is the trace→eval connection that makes continuous eval work. (`ADALINE.md §4.4`)

---

## 5. Incident → Regression in One Line

### 5.1 The pattern: signal trigger on trace completion

The Laminar trigger fires immediately after each trace batch is processed (`check_and_push_signals` in `process_span_messages`). The default "Failure Detector" signal trigger is:
```
total_token_count > 1000 AND root_span_finished == true
```
So: trace finishes → within one processing cycle → signal evaluates → if `identified=true` → `signal_event` written → alert fires (Slack/email/webhook). The whole chain runs in production on every matching trace. (`LAMINAR.md §6.2`)

**Distributed dedup lock:** `SIGNAL_TRIGGER_LOCK_TTL_SECONDS = 3600` prevents the same trace from triggering the same signal twice in a 1-hour window, even under concurrent processing. (`LAMINAR.md §19.§5.7`)

**Alert filtering:** alerts filter by `severity` from `alerts.metadata.severities`. Default if missing: CRITICAL only (most restrictive for historical alerts). (`LAMINAR.md §6.9`)

### 5.2 The W&B pattern: content-addressed versioning as regression anchor

W&B artifact dedup: `digestAlgorithm: MANIFEST_MD5, enableDigestDeduplication: true` on `createArtifact`. Manifest digest = `md5("wandb-artifact-manifest-v1\n" + Σ sorted "path:entry_digest\n")`. Same files = same manifest digest = no bytes uploaded = same artifact version. `birth_artifact_id` pins which version first stored each blob. (`WEIGHTS_AND_BIASES.md §16`)

**Regression pattern:** artifact version A (a dataset or model) is linked to a run via `UseArtifactRecord` → lineage graph. If a new run produces worse metrics, you can trace back to the artifact change that caused it by following the lineage. This is the "incident → regression in one line" for ML training, not LLM agents.

### 5.3 The Adaline pattern: `runEvaluation` flag + continuous eval

`runEvaluation: true` on `POST /logs/span` forces evaluation regardless of sample rate. An incident handler can re-submit a span from an incident with `runEvaluation: true` and get an immediate eval result to confirm whether the evaluator would have caught it. (`ADALINE.md §4.6`)

---

## 6. Key Anti-Patterns (what NOT to do)

1. **Skip field tags in the eval agent prompt.** Without `[complete]`/`[truncated]`/`<omitted>`/`<empty>` tags, the agent burns tool calls on data that's already visible. Laminar measured: "A single unnecessary search can double the total cost." (`LAMINAR.md §19.§3.1`)

2. **Run the eval agent on raw traces.** Raw agent traces are 50–200k tokens. The filter → summarize → compress pipeline gets to 5–20k. Skipping this makes eval 10–40× more expensive AND less accurate (the agent gets confused by noise). (`LAMINAR.md §6.6-6.8`)

3. **Chain multiple tool searches before submitting.** The Laminar system prompt explicitly forbids it: "After receiving search results, call submit_identification on your next response. Do NOT chain multiple searches." Every retrieval re-sends the full conversation context. (`LAMINAR.md §19.§3.1`)

4. **Use JSON for long-context eval prompts.** XML/tag envelopes are 6× more token-efficient than JSON for the same accuracy (CodeRabbit measured 18.3K vs 110K tokens). JSON structural chars accumulate; at 100K+ tokens, tagged text outperforms nested JSON for LLM navigation. (`CODERABBIT.md §3.3-3.4`)

5. **Trace streamed calls as multiple spans.** Streamed responses omit `usage` until the final chunk; if you record each SSE delta as a separate span you lose token counts. The Weave streaming accumulator folds all deltas into one logical span with reconstructed `usage`. (`WEIGHTS_AND_BIASES.md §8.2`)

6. **Miss the ms-vs-seconds timestamp split.** Adaline's `EvaluationRunTimeline.submittedAt` is Unix ms; `startedAt`/`finishedAt` are Unix seconds. A consumer that assumes uniform ms will have 1000× latency calculation errors. (`ADALINE.md §2.3`)

---

## 7. Byte-Path Implications for Lancelot

### Already in Lancelot:
- `lancelot.context` ContextVars (`cost_usd`, `tokens_in/out`, `agent_id`, `request_id`, `trace_id`) — the per-request span root exists
- `lancelot.upstream_metrics` — provider detection + token extraction across 3 provider shapes + OTel histograms `lancelot.upstream.*`
- `TransportPool` auto-injects `traceparent` → W3C trace parent propagation (same mechanism as Weave's `trace_id`/`parent_id` span tree)
- `lancelot.http.stream.ttft` — TTFT metric (the AI-streaming UX metric the corpus validates as THE user-facing metric)
- `lancelot.canonical.dumps_canonical` — stable JSON bytes for KV-cache prefix stability

### What the corpus adds / changes for Lancelot's eval layer:

**Eval-as-run needs a second turn prompt structure.** The first turn carries compressed trace data (with field tags); the second turn brings in the user-defined assertion prompt. These must be separate so the trace-awareness instructions stay stable while the assertion is fully user-defined. Lancelot has the transport; it needs the two-prompt pattern.

**The filter-then-compress pipeline changes how streaming traces are used.** Lancelot's `@app.stream` and R3 pump give raw streaming. For trace-based evals, raw streamed content needs Jaccard-based tool-dedup (`TOOL_DEDUP_OVERLAP_THRESHOLD = 0.75`) and per-message truncation before the eval agent sees it.

**W&B's streaming accumulator** (fold SSE deltas, reconstruct `usage`) is needed for any Lancelot eval that runs against streamed LLM calls. `TransportPool.stream` gives the byte path; the accumulator is the eval-correctness layer on top.

**The `structural_skeleton_hash`** (strip numbers/IDs/timestamps/stopwords before hashing) is how you get cache hits across traces that differ only in runtime parameters. Lancelot's `dumps_canonical` handles JSON stability; skeleton hashing handles prompt-structure stability. These are complementary: one for KV-cache, one for eval-cache.

**OTel `gen_ai.*` span normalization across providers** is the missing layer between `lancelot.upstream_metrics` (which does provider detection + token counts) and a full process eval. Laminar's `spans.rs` (4459 lines) is the reference implementation. The minimum needed: map each provider's `usage` shape to canonical `gen_ai.usage.{input_tokens,output_tokens,cached_tokens}` and set `gen_ai.operation.name` correctly on each call.

**The `runEvaluation` flag pattern** (Adaline: force eval on a specific span regardless of sample rate) maps directly onto Lancelot's per-request ContextVar pattern. A `lancelot.context.force_eval` ContextVar would let a request handler or middleware tag specific calls for immediate eval regardless of the sampling rate.

---

## 8. Exact Constants to Port

| Constant | Value | Source |
|---|---|---|
| `TRUNCATE_THRESHOLD` | 2048 chars | Laminar spans.rs |
| `LLM_MESSAGE_MAX_CHARS` | 3000 chars/string | Laminar spans.rs |
| `LLM_INPUT_TOTAL_MAX_CHARS` | 8192 chars | Laminar spans.rs |
| `TOOL_DEDUP_OVERLAP_THRESHOLD` | 0.75 (Jaccard) | Laminar spans.rs |
| `MAX_ALLOWED_STEPS` | 5 (env-overridable) | Laminar signals/mod.rs |
| `FILTER_CACHE_TTL` | 7 days | Laminar filter.rs |
| `SUMMARY_CACHE_TTL` | 30 days | Laminar summarize.rs |
| `SIGNAL_TRIGGER_LOCK_TTL` | 3600s | Laminar processor_hook.rs |
| Weave batch size | 100 spans | async_batch_processor.py |
| Weave batch interval | 1.0s | async_batch_processor.py |
| Weave request byte limit | 31 MiB | http_utils.py |
| Max token context after filter | 5–20k tokens | Laminar design principle |
| Raw trace before filter | 50–200k tokens | Laminar design principle |
| filter-gen skip threshold | 1,000,000 chars | Laminar filter.rs |
| CONTEXT_PADDING (fuzzy search) | 256 chars | Laminar search.rs |
| MAX_MATCHES_PER_SEARCH | 5 | Laminar search.rs |
| CodeRabbit review timeout | 3,600,000 ms (1h) | bin:af_pretty.js |
| CodeRabbit diff cap | 20 MB per file | bin:af_pretty.js |
| Predecessor model temp | 0.05 | ai-pr-reviewer action.yml |
| Predecessor concurrency | 6 (LLM) + 6 (GitHub) | ai-pr-reviewer review.ts |
| Laminar CH async_insert | `async_insert=1, wait_for_async_insert=1, with_validation(false)` | main.rs |
