# Together AI — Mechanism-Level Distillation for Lancelot

**Source:** `researchfms/teardowns/TOGETHERAI.md` (Round 1 + Round 2, 2026-05-15)
**SDK RE'd:** `together==2.14.0` (Stainless-generated, 12,264+ lines read)
**Live probing:** May 2026, api.together.ai

---

## 1. What Together Actually Is (positioning anchor)

Together is a **five-product AI cloud**: serverless inference, dedicated endpoints, fine-tuning, raw GPU cluster rental, and container deployment (Jigs). It is not an API proxy. The inference layer is a proprietary vLLM-derived engine with custom CUDA kernels (FlashAttention-3/4, megakernel, fused MoE routing) and ATLAS adaptive speculative decoding. The moat is the engine + the FlashAttention research relationship (Together sponsors Tri Dao's Stanford group; they get FA-4 on day zero).

For Lancelot's purposes: Together is a **provider to call**, not a model to replicate. The actionable mechanisms are in the API contract, the error taxonomy, the cost levers (quantization tiers, batch API, dedicated vs serverless), and the streaming/latency architecture.

---

## 2. API Shape — OpenAI-Compatible Extension Surface

**Source: `together/types/chat/completion_create_params.py` — KNOWN verbatim.**

Together's `POST /v1/chat/completions` is OpenAI-compatible plus these production-significant extensions:

### Parameters Lancelot's model adapter / ProviderProfile must handle

| Parameter | Type | Semantics |
|-----------|------|-----------|
| `min_p` | `float` | Keep tokens with prob ≥ `min_p × top_token_prob`. vLLM `SamplingParams` name. Fingerprints Together/vLLM backend. |
| `top_k` | `int` | Hard top-K cutoff. Non-standard OpenAI. |
| `repetition_penalty` | `float` | Anti-repetition. Different from OpenAI `frequency_penalty`/`presence_penalty`. |
| `chat_template_kwargs` | `object` | Raw kwargs into HF `apply_chat_template`. E.g. `{"enable_thinking": True}` for DeepSeek thinking toggle. **Critical for reasoning-model control.** |
| `context_length_exceeded_behavior` | `"truncate" \| "error"` | Together-specific. Most providers just 400 on overflow; Together lets user choose silent truncation. Important for agent loops with long context. |
| `compliance` | `"hipaa"` | Server-side routing to HIPAA-eligible pods. Per-request, not per-account. |
| `safety_model` | `str` | Post-generation moderation model name (e.g. `"meta-llama/Llama-Guard-4-12B"` at $0.20/M tokens). Server-side composition — single API call chains inference + moderation. |
| `reasoning` | `{"enabled": bool}` | Alt to `reasoning_effort` for models with reasoning toggle. |
| `disable_speculative_decoding` | `bool` | Per-endpoint opt-out of ATLAS. Relevant for short-input workloads where spec-decode overhead hurts. |

### What this means for Lancelot's model adapter

The `chat_template_kwargs` parameter is the cleanest extension knob: Lancelot's ProviderProfile for Together should thread it through as an opaque passthrough. `{"enable_thinking": True}` is the concrete case — agents doing reasoning-model calls need to toggle this without knowing the backend. Lancelot's adapter can surface this as `provider_kwargs={"chat_template_kwargs": {...}}`.

`context_length_exceeded_behavior: "truncate"` is worth defaulting to `"error"` in production agents (silent truncation destroys context without signaling the agent loop). Lancelot should default to `"error"` and let the user opt into truncation explicitly.

---

## 3. Request/Trace Correlation — `x-request-id` Format

**Source: §R2.1, live probing.**

Together's gateway response header:
```
x-request-id: oj6zp9T-2kFHot-9fbd0cb1080ce4c4
```

Three-segment composite:
- **Segment 1** (7-char base62): Together's internal request ID (generated at the Node.js gateway).
- **Segment 2** (6-char base62): gateway instance/pod ID — stable across requests hitting the same replica.
- **Segment 3** (16-char hex): Cloudflare ray ID (`cf-ray` header value, byte-for-byte).

`x-api-received` header gives the gateway receive timestamp (ISO 8601 with milliseconds). This enables end-to-end latency profiling: `client_send_time → x-api-received → response_received`.

**Lancelot byte-path implication:** `TransportPool` already auto-injects `x-request-id` on outbound calls and propagates `traceparent`. The Together response's own `x-request-id` should be captured and forwarded to the agent's context (for correlated support tickets, tracing). `lancelot.context.trace_id` can absorb Together's segment 1 as a downstream span ID. **No new machinery needed — TransportPool just needs to log/capture the response `x-request-id` alongside the outbound one.**

---

## 4. Error Taxonomy — Verbatim Format

**Source: §R2.2, live probing.**

```json
{
  "id": "oj4jpmy-2kFHot-9fba9cb99fbc8989",
  "error": {
    "message": "...",
    "type": "invalid_request_error",
    "param": null,
    "code": "invalid_api_key"
  }
}
```

Observed `code` values: `invalid_api_key`, `missing_api_key`. The `id` field always present (even on auth errors). The `type` field maps to:
- `invalid_request_error` — 4xx, client error, do not retry
- (inferred from SDK structure) `server_error` — 5xx, transient, retry eligible

**Bad JSON payload** (no `id` field, no `code`):
```json
{"error": {"message": "Invalid JSON payload", "details": "Unexpected token 'o', \"notjson\" is not valid JSON"}}
```
The `Unexpected token 'o'` wording is verbatim Node.js `JSON.parse` error — KNOWN fingerprint of the Node.js gateway.

**Lancelot byte-path implication:** `lancelot.upstream_metrics` already classifies Together errors via the taxonomy `ok/quota/timeout/permanent/transient/upstream_5xx`. The Together error shape is OpenAI-compatible; the existing provider-detect + token-extraction logic handles it. The one addition worth considering: capturing the response `id` field as a correlation key in the error payload surfaced to the agent (currently the error taxonomy drops it).

---

## 5. SDK Constants — Retry / Timeout Defaults

**Source: `together.lib.constants`, `together._client`, KNOWN verbatim.**

```python
MAX_SESSION_LIFETIME_SECS = 180      # 3-minute session max
MAX_CONNECTION_RETRIES = 2
MAX_RETRIES = 2
INITIAL_RETRY_DELAY = 0.5            # 500ms
MAX_RETRY_DELAY = 8.0                # 8 seconds
```

Default client timeout: **600 seconds** (10 minutes) — sized for fine-tuning job polling, NOT for inference. For inference use, override to a sane value (e.g., 30s for non-streaming, or use streaming with TTFT deadline).

**Lancelot byte-path implication:** `TransportPool` already implements exp-backoff + Retry-After honoring. The Together constants (0.5s initial, 8s max, 2 retries) are conservative — fine for Together's use case (fine-tune polling is a long-polling pattern). For inference calls, `TransportPool`'s defaults are already appropriate. No change needed; just document the 600s default as a gotcha when creating a raw httpx client.

---

## 6. Batches API — 50% Discount Lever

**Source: SDK `batches` resource, §R2.6 pricing.**

Together's `POST /v1/batches` submits a batch of inference requests (from a pre-uploaded JSONL file) at **50% discount** on most models. Non-time-sensitive workloads (eval runs, dataset annotation, bulk embeddings) should route here, not to the streaming endpoint.

The lifecycle mirrors OpenAI's Batch API: `create → list → retrieve → cancel`. Files must be pre-uploaded via `client.files.create()`.

**Lancelot byte-path implication:** `@app.batch` is Lancelot's *inbound* batch coalescing primitive (groups multiple HTTP requests into one upstream call, ~12× throughput). The Together Batch API is an *outbound* discount mechanism (single API call processes many requests asynchronously at half price). These are complementary:
- Lancelot `@app.batch` coalesces real-time agent tool calls into fewer upstream calls.
- Together Batch API discounts asynchronous bulk work.

A practical pattern: Lancelot's eval harness or annotation pipeline routes through `TransportPool` to `POST /v1/batches`, while the live agent call path uses serverless streaming. This is a real cost lever (50% off on bulk eval runs).

---

## 7. Quantization Tiers — The Reference/Turbo/Lite Cost Lever

**Source: §Quantization Strategy, §R2.7 pricing, KNOWN from blog.**

Together exposes precision-vs-quality tiers via model naming convention:

| Suffix | Precision | Example | Input $/M |
|--------|-----------|---------|-----------|
| (none / Reference) | FP16/BF16 | `Llama-3.3-70B-Instruct` | ~$1.80 |
| Turbo | FP8 (QuIP incoherence processing) | `Llama-3.3-70B-Instruct-Turbo` | $0.88 |
| Lite | INT4 | (not all models) | ~$0.10 |

The **Turbo tier is the production sweet spot**: FP8 with QuIP outlier handling ("incoherence processing" — random orthogonal transform before quantization). Together claims "near-lossless" quality. The 2× price difference (Turbo vs Reference) maps directly to a 2× GPU footprint difference (FP8 = half the memory of FP16).

**Quantized KV-cache**: Together supports quantizing both model weights AND KV-cache, doubling effective context capacity. For agents with long context, this is relevant — 512K context on DeepSeek-V4-Pro is only economically feasible with quantized KV-cache.

**Lancelot byte-path implication:** Lancelot's `upstream_metrics.calc_cost_usd` needs the model name to look up rates (no rates shipped with Lancelot). The model name suffix (`-Turbo`, `-Lite`) is the signal for which rate row to use. The ProviderProfile / model adapter should normalize model IDs and expose the tier as metadata. `LANCELOT_LLM_RATES` env JSON handles the rate table; the model ID normalization is in the adapter layer (not yet built).

---

## 8. Dedicated Endpoints — The `disable_speculative_decoding` Knob

**Source: `together/types/endpoint_*.py`, §Dedicated Endpoints, KNOWN.**

```python
create_endpoint_params = {
    "autoscaling": {"min_replicas": 1, "max_replicas": 10},
    "hardware": "1x_h100_80gb",
    "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "disable_speculative_decoding": False,  # default = speculative decoding ON
    "inactive_timeout": 30,  # auto-stop after 30 min idle; 0/null = always-on
}
```

`disable_speculative_decoding: True` is the escape hatch when ATLAS hurts: short-input workloads (classification, structured extraction) where the speculator's verification overhead costs more than the accepted tokens save. At P50 input length <100 tokens, spec-decode can add latency.

`inactive_timeout` (minutes, 0/null = always-on) — for an agent serving periodic traffic, set to 5–10 minutes to avoid idle costs without hitting cold-start latency on every burst.

**Lancelot byte-path implication:** `TransportPool` routes to a base URL. For dedicated endpoints, the URL changes to `https://{adjective-animal}.api.together.ai/v1`. Together assigns Heroku-style names (e.g., `merrymeerkat.api.together.ai`). Lancelot's `TransportPool` can hold multiple named pools pointing to different dedicated endpoints — no new machinery, just multiple pool instances with different `base_url`.

---

## 9. ATLAS Adaptive Speculative Decoding — Architecture (NOT to replicate, but to understand latency SLAs)

**Source: §R2.4, Together engineering blog, KNOWN.**

The architecture matters for latency reasoning:

```
Static speculator (broad corpus, depth 4-8 tokens)
         +
Adaptive speculator (retrained on live traffic, depth 2-4 tokens)
         |
Confidence-aware controller (small NN):
  inputs: per-token target-model confidence, current acceptance rate, workload signal
  outputs: which speculator to use + lookahead depth K
         |
         v
Target model verifies K candidate tokens in one forward pass
```

**Verbatim performance (KNOWN):**
- 500 TPS on DeepSeek-V3.1 in fully-adapted scenario (vs 105 TPS FP8 baseline = 4.8× speedup)
- 460 TPS on Kimi-K2
- In RL training: acceptance rate rose from <10% to >80% over 1.4k training steps, yielding 60% reduction in training time

**The on-the-fly vibe-coding specialization**: after ~100 turns in a coding session, the adaptive speculator has retrained enough on the current codebase's patterns that acceptance rate climbs to >80%. This is why Together's TTFT gets *better* during a long session, not worse.

**Lancelot byte-path implication:** ATLAS operates entirely server-side. Lancelot cannot influence it except via:
1. Setting `disable_speculative_decoding: True` on dedicated endpoints when spec-decode hurts.
2. Routing long sessions to the *same* dedicated endpoint (via `session_affinity`) so the adaptive speculator learns the session's patterns. `Response.set_session_affinity(key)` + Together dedicated endpoint = the technical underpinning for vibe-coding sessions that get faster over time.

This is a **real Lancelot positioning story**: Lancelot's session affinity primitive (`set_session_affinity`) combined with Together's ATLAS = measurably better TTFT for long agent sessions. Neither product alone achieves it.

---

## 10. Evals API — Together's Managed Eval Framework

**Source: SDK `evals.py`, `eval_*.py` types, §Evals API, KNOWN.**

`POST /v1/evals` accepts:
- A dataset (file ID, uploaded via `/v1/files`)
- An evaluator config (judge model + rubric — details opaque from SDK alone)
- A model under test

Returns a job ID that tracks like a fine-tune job (pending → running → completed). Results accessible via `evals.status()`.

This is **rare** among inference providers — most don't ship an eval framework as an API. The `evaluator.together.ai` subdomain (visible in CT logs) is the customer-facing eval dashboard.

**Honest assessment:** The SDK reveals the lifecycle but not the evaluator config schema. The eval framework is likely a wrapper around LLM-as-judge with pre-built rubrics. It is a tactical convenience product, not a principled eval system. For Lancelot's eval primitives, this is a signal that managed eval-as-a-job (async batch evaluation over a dataset) is a real pattern — but the judge config needs to be Lancelot-controlled, not Together-owned.

**Lancelot byte-path implication:** Lancelot's `@app.batch` + Together's Batch API (50% discount) + a judge model call → eval pipeline. Lancelot can offer `lancelot.evals.run(dataset, judge_fn, model_fn)` that uses the batch discount path internally. This is a composable pattern from existing primitives, not a new API surface.

---

## 11. Code Sandbox / Code Interpreter

**Source: SDK `code_interpreter`, §R2.10, KNOWN.**

Pricing: $0.0446/vCPU-hour + $0.0149/GiB-hour, OR $0.03/60-min session (flat session rate, simpler).

Runtime: inferred Firecracker microVMs (session-based, sub-second boot, per-vCPU billing pattern matches Firecracker/Lambda/E2B semantics).

Use in agent tool calls: `client.code_interpreter.execute(code="print('hello')", session_id=...)`. The `sessions.list()` endpoint returns active sessions — the agent manages session lifecycle.

**Lancelot byte-path implication:** Code sandbox calls are outbound tool calls from the agent. `TransportPool` handles the HTTP; `@app.tool` can wrap `client.code_interpreter.execute()` as a tool. The session lifecycle (session_id across turns) maps to `session_affinity` — keep the agent's tool calls on the same code interpreter session. **Already handled by existing primitives.**

---

## 12. Fine-Tuning Lifecycle — Operational States

**Source: `finetune_response.py` (205 lines), §R2.9, KNOWN.**

```
pending → queued → running → compressing → uploading → completed
                             cancel_requested → cancelled
                             (any state) → error
```

The `compressing` state is non-trivial: Together compresses checkpoints before uploading (custom dedup + quantization to deployment precision + optimizer state removal). For large models (70B+), this phase can take 10-30 minutes.

`from_checkpoint: str` — resume from intermediate checkpoint. Format: `{job_id}/checkpoint-{step}`. Critical for multi-day training runs that hit preemption.

`from_hf_model: str` + `hf_model_revision: str` — initialize fine-tune from a HuggingFace model. No data upload needed for the base model.

`wandb_project_name: str` — first-class W&B integration (not a webhook, it's a built-in parameter). Auto-populates `wandb_url` in the response.

DPO variant params (KNOWN, verbatim from SDK):
```python
dpo_beta: float                          # standard DPO
dpo_normalize_logratios_by_length: bool  # length-normalized DPO
dpo_reference_free: bool                 # SimPO-style reference-free
rpo_alpha: float                         # RPO (Reward Preference Optimization)
simpo_gamma: float                       # SimPO gamma
```

Data format constants (from `together.lib.constants`, KNOWN):
```python
DatasetFormat = Enum("general", "conversation", "instruction", "preference_openai")
JSONL_REQUIRED_COLUMNS_MAP = {
    "general": ["text"],
    "conversation": ["messages"],
    "instruction": ["prompt", "completion"],
    "preference_openai": ["input", "preferred_output", "non_preferred_output"],
}
PARQUET_EXPECTED_COLUMNS = ["input_ids", "attention_mask", "labels", "position_ids"]
```

**Lancelot byte-path implication:** Fine-tuning jobs are long-running async operations. The pattern is: `TransportPool` submits the job, returns a job ID, agent polls `fine_tuning.list_events(id)` for status. This maps cleanly to `TransportPool.spawn_child` + polling. No new Lancelot primitives needed; the fine-tune workflow is a standard async-job pattern on top of existing transport.

---

## 13. Jigs Autoscaling — The `QueueBacklogPerWorker` Default

**Source: `jig_deploy_params.py`, §Jigs, KNOWN.**

Three autoscaling metric types:

```python
AutoscalingHTTPAutoscalingConfig:
    metric: "HTTPTotalRequests" | "HTTPAvgRequestDuration"
    target: float        # default 100 requests OR 500 ms duration
    time_interval_minutes: int   # default 10

AutoscalingQueueAutoscalingConfig:
    metric: "QueueBacklogPerWorker"
    model: str
    target: float        # default 1.01

AutoscalingCustomMetricAutoscalingConfig:
    metric: "CustomMetric"
    custom_metric_name: str   # Prometheus metric name
    target: float
```

The **`QueueBacklogPerWorker: 1.01`** default is Together's recommended production setting: "spin up a new replica when each worker has more than ~1 request queued." This is aggressive — it biases toward latency over cost, spinning up replicas fast.

The **`CustomMetric` autoscaler** (any Prometheus metric as scale trigger) is rare. Pattern: expose a `lancelot.upstream_metrics` histogram (e.g., `lancelot.upstream.latency_ms`) as a Prometheus metric, use it as the Jigs scale signal. Lancelot already emits OTel histograms under `lancelot.upstream.*` — with a Prometheus exporter, these can drive Together Jigs scaling.

**Lancelot byte-path implication:** If deploying a Lancelot application as a Together Jig (containerized serving), the recommended scaling config maps to:
- Use `QueueBacklogPerWorker` at 1.01 for latency-sensitive agent serving.
- Use `HTTPAvgRequestDuration: 500ms` for cost-sensitive batch processing.
- `CustomMetric` → `lancelot.upstream.queue_wait_ms` P95 as a scale trigger (new pattern; Lancelot already emits this metric).

---

## 14. GPU Cluster Architecture — Operational Realities

**Source: §R2.3, §R2.12, `cluster_create_params.py`, KNOWN.**

Key facts from the 1,665-subdomain + SDK analysis:

1. **`num_gpus` must be multiple of 8** — confirms 8× GPU per node (NVIDIA HGX standard). Minimum useful cluster = 8 GPUs.
2. **SCHEDULED_CAPACITY billing** = book a time window (`reservation_start_time`, `reservation_end_time`). The only major AI cloud to expose this. Maps to "conference room reservation" for planned training jobs.
3. **`kube_config: str`** field in cluster response — Together hands you raw kubeconfig YAML. You get `kubectl apply` access to your cluster directly.
4. **162-node cluster** (`b24997a1-001` through `b24997a1-162`, confirmed in CT logs) = 1,296 H100 GPUs = ~$32.4M hardware cost. Together operates at this scale.
5. **`H100_SXM_INF` SKU** = inference-optimized H100 (likely ECC-disabled for throughput, higher TDP). Request this for inference-only dedicated workloads.
6. **Slurm as first-class product** (alongside Kubernetes). `cluster_type: "SLURM"` is the academic/HPC research customer path.

**Lancelot byte-path implication:** GPU cluster APIs are not in Lancelot's scope (Lancelot is the serving layer, not the provisioner). Relevant context: Together's cluster design is why `SO_REUSEPORT` + multiprocess Lancelot deployment is the right model on Linux — it maps to Together's own multi-worker GPU serving architecture.

---

## 15. Pricing Summary — The Cost Levers

**Source: §R2.7, §R2.8, pricing page May 2026, KNOWN verbatim.**

### Inference cost levers (ranked by impact)

1. **Batch API**: 50% discount on bulk/async workloads. Highest impact for eval pipelines.
2. **Turbo tier (FP8)**: ~2× cheaper than Reference for the same model. `Llama-3.3-70B-Instruct-Turbo` at $0.88 vs ~$1.80 Reference.
3. **Lite tier (INT4)**: Cheapest, for cost-optimized scale. `Qwen3.5-9B` at $0.10/M input.
4. **`context_length_exceeded_behavior: "error"`**: Prevents silent context truncation that wastes tokens and produces wrong outputs.
5. **`safety_model` chaining**: $0.20/M tokens for Llama-Guard-4-12B moderation (cheaper than a separate API call with client-side orchestration).

### Fine-tuning: Full vs LoRA

Full fine-tuning is consistently **2.5× the LoRA cost** per band:
- Up to 16B: LoRA SFT $0.48/M, Full SFT $1.20/M
- 17-69B: LoRA SFT $1.50/M, Full SFT $3.75/M
- 70-100B: LoRA SFT $2.90/M, Full SFT $7.25/M

### Reserved GPU pricing (31-90 day tier)

H100 on-demand → 7-30d reserved → 31-90d reserved → 91-180d reserved:
$3.99 → $3.45 → $3.09 → $2.95 (26% discount at 91-180 days).

---

## Honest Assessment of Signal Quality

### Durable primitives (3+ model generations)
- The OpenAI-compatible extension surface (`min_p`, `top_k`, `chat_template_kwargs`) — stable because it mirrors vLLM's SamplingParams, not Together-specific.
- The batch discount pattern (50% off async) — structural economic incentive, not a product feature.
- The quantization tier naming convention (Turbo/Lite) — stable because it reflects real hardware/precision tradeoffs.
- Session affinity + ATLAS = TTFT improvement for long sessions — this is a durable architectural insight.

### Tactical / depreciating (1-2yr model clock)
- Specific model IDs and prices — churn every 3-6 months. Do not hardcode.
- ATLAS's 400-500 TPS numbers — will be obsoleted by better hardware (B200, GB300) and better algorithms.
- FA-4 implementation details (TMEM, CuTe-DSL, tile sizes) — GPU-generation-specific.

### What is NOT in the source (honest gaps)
- ATLAS confidence-aware controller architecture (likely a 2-layer MLP, unconfirmed).
- The actual inference engine scheduler (closed-source beyond the published blog claims).
- Eval API evaluator config schema (opaque from SDK alone).

---

## Byte-Path Summary for Lancelot

| Together mechanism | Lancelot status |
|-------------------|-----------------|
| `x-request-id` composite (3-segment trace) | `TransportPool` injects outbound ID; should capture Together's response ID for downstream correlation |
| `x-api-received` timestamp | New: capture this in `TransportPool.stream()` response headers for TTFT measurement improvement |
| Batch API 50% discount | `TransportPool` handles the HTTP; no new primitives needed; pattern: eval/annotation routes to `/v1/batches` |
| `context_length_exceeded_behavior: "error"` | Default to `"error"` in ProviderProfile for Together; expose as opt-in override |
| Session affinity + ATLAS TTFT improvement | `Response.set_session_affinity(key)` already built; the Together story is the justification |
| `chat_template_kwargs` passthrough | Needed in model adapter `provider_kwargs` passthrough (not yet built) |
| `disable_speculative_decoding` on dedicated endpoints | Endpoint-level config, not per-request; expose in ProviderProfile |
| Jigs `CustomMetric` autoscaling via Prometheus | `lancelot.upstream.*` OTel histograms already emitted; Prometheus exporter → Jigs scale signal |
| Fine-tune async job polling | `TransportPool.spawn_child` + polling; no new primitives |
| Code sandbox session affinity | `session_affinity` maps to sandbox session_id; already covered |
| Error `code` field capture | `upstream_metrics` error taxonomy should surface the Together `error.code` string |
