# Character.AI — Mechanism-Level Distillation

**Source:** `researchfms/teardowns/CHARACTER_AI.md` (Rounds 1–3, 2026-05-20)
**Focus areas per task:** Prompt Poet cache-aware truncation, JudgeJudy 3-judge panel, `is_pinned` / `TooManyPins` pinning, sticky-session-per-chat_id.

---

## 1. Prompt Poet — cache-aware context truncation

**Source:** `CHARACTER_AI.md §C3.5`, from `character-ai/prompt-poet` (MIT, `pip install prompt-poet`).

### 1.1 Core data model

```python
@dataclass
class PromptPart:
    name: str
    content: str
    role: str = "user"
    tokens: list[int] | None = None
    truncation_priority: int = 0   # 0 = NEVER truncated
    sections: list[PromptSection] | None = None

@dataclass
class TruncationBlock:
    left: int
    right: int
    truncation_priority: int
    total_tokens: int
```

A prompt is **a list of `PromptPart` objects**. The list-of-parts structure is what makes cache-aware truncation possible — each part has an independent priority. Source: `prompt-poet/prompt_poet/prompt.py:21-62`.

### 1.2 The cache-aware truncation knob

```python
def _calculate_num_tokens_to_truncate(self, token_limit, truncation_step) -> int:
    num_surplus_tokens = max(0, self._total_tokens - token_limit)
    return math.ceil(num_surplus_tokens / truncation_step) * truncation_step
```

Source: `prompt.py:190-236`.

The mechanism: instead of truncating exactly to `token_limit`, round up to the **next multiple of `truncation_step`**. If `token_limit=8192`, `truncation_step=1000`, and you have 9,100 tokens, truncate 1,000 (not 908), leaving 8,100. Next turn at 9,300 tokens still lands at the same 8,100-token boundary. The prefix up to that boundary is byte-identical across turns → **cache-hits the KV prefix cache** from the previous turn.

This is the tradeoff knob:
- `truncation_step=1` → naive truncation, recomputes prefix every turn.
- `truncation_step=1000` → cache-aware, only re-prefills when the boundary shifts (overshoot = up to `truncation_step-1` extra tokens dropped).

The companion blog post says explicitly: "the tradeoff: we often truncate more than we strictly need to" (`blog.character.ai/prompt-design-at-character-ai/`).

### 1.3 Truncation priority semantics (inverted — eviction order, not retention order)

```python
truncation_blocks.sort(
    key=lambda marker: marker.truncation_priority, reverse=True
)
```

Source: `prompt.py:437-490`.

**Higher priority = evicted first.** Production template conventions:
- `truncation_priority=0` — system instructions, character definition + greeting, current user query. NEVER truncated.
- `truncation_priority=10` — oldest chat history. First to go.
- `truncation_priority=5` — recent (but not latest) messages. Medium priority.

The character-definition and persona prefix are always priority-0 — they anchor the prefix cache (95% hit rate in production on that prefix).

### 1.4 Character definition injection (line-per-message, not system-prompt blob)

```python
if definition := character.get("definition"):
    for line in definition.split("\n"):
        messages.append(CAIMessage(author="", text=line))
```

Source: `cai_helpers.py:48-70` (the "Hermes generation" helper — "Hermes" is Character.AI's internal codename for their LLM generation pipeline).

The character definition is not a monolithic system prompt — each newline is emitted as a separate message with empty author. This matches the training data format. The model was trained on dialog-as-message-stream, not dialog-as-system-prompt.

### 1.5 Pre-truncation coarse filter (Hermes)

```python
def pretruncate_messages(messages, token_limit):
    """Pretruncates messages for Hermes generation."""
    if token_limit < 0:
        return messages
    message_truncation_step = token_limit // 10
    while len(messages) > 2 * message_truncation_step:
        messages = messages[message_truncation_step:]
    return messages
```

Source: `cai_helpers.py:88-94`.

First-pass coarse truncation: drop chunks of `token_limit // 10` messages off the front while message count exceeds `2 × (token_limit // 10)`. This is a fast O(1) filter applied *before* the more expensive token-level Prompt Poet pass.

### 1.6 Open-source tokenizer default vs production

`prompt-poet` defaults to `o200k_base` (GPT-4o tokenizer). This is the open-source default for third-party model prototyping. Character.AI's production tokenizer is `character-tech/tokenizers` — a fork of HuggingFace's Rust tokenizer crate (last push Jan 2024). Use your own model's tokenizer, not this default.

---

## 2. JudgeJudy — 3-judge eval panel

**Source:** `CHARACTER_AI.md §C3.10`, from `character-ai/judgejudy` (Apache-2.0, recently open-sourced April 2026, 3★).

### 2.1 3-judge panel (the production weights)

```yaml
# examples/multi_judge.yaml
evaluators:
  - judge: anthropic/claude-sonnet-4-6    weight: 0.4
  - judge: openai/gpt-4o                  weight: 0.4
  - judge: google/gemini-2.5-pro          weight: 0.2
```

Source: `§C3.10`, `character-ai/judgejudy/examples/multi_judge.yaml`.

**Cross-validation discipline:** generator and judge must be different model families. Default config: GPT-4o generates, Claude judges (`examples/text_eval.yaml`). The asymmetric weighting (Gemini gets 0.2 vs 0.4) is consistent with Claude + GPT-4o having longer calibration history.

### 2.2 Single-judge pointwise rubric

```yaml
evaluators:
  - name: "quality-judge"
    type: ai_judge
    provider: anthropic
    model: claude-sonnet-4-6
    mode: pointwise
    rubric: |
      Evaluate the response on these criteria:
      - Accuracy: ...
      - Clarity: ...
      - Relevance: ...
      - Conciseness: ...
    dimensions: [accuracy, clarity, relevance, conciseness]
    scale: [1, 5]
    threshold: 0.7
  - name: "bertscore"
    type: metric
```

Source: `§C3.10`, `examples/text_eval.yaml`. Pass bar: **0.7 out of 1.0 normalized** from the 1-5 scale.

### 2.3 Architecture

```
pkg/
├── judgejudy/    # Run() + GenerateReport()
├── evaluator/    # composite, ai_judge, metric evaluators
├── provider/     # 16 generation providers (OpenAI, Anthropic, Google, Together, Replicate, Ollama, vLLM, Cartesia, ElevenLabs, fal.ai, WaveSpeed, ...)
├── calibrate/    # AI-judge-vs-human calibration, rubric improvement suggestions
├── report/       # HTML report generation
└── store/        # pluggable persistence (SQLite default, custom backends)
```

16 supported generation providers. Calibrate module feeds human-vs-AI disagreement back into rubric refinement. Go-based harness.

---

## 3. `is_pinned` / `TooManyPins` — pinned turn (Memo) system

**Source:** `CHARACTER_AI.md §5.3`, `§3.12`.

### 3.1 Data model

```python
@dataclass
class CAIMessage:
    author: str
    text: str
    type: int = 0    # 0=Unspecified, 1=Human, 2=Character, 3=System
    is_pinned: bool = False
```

Source: `cai_helpers.py:13-21`. The `is_pinned` flag is a first-class field on every message. Pinned messages are permanent context lines that survive any truncation — they are injected into every subsequent generation regardless of dialog length.

### 3.2 Pin mechanics

The WS command `set_turn_pin` (`:73102`) toggles a turn's pin state. Pinned turns appear in the prompt template under a dedicated `{{#if pinned_memos}}` section with `truncation_priority=0` (never evicted).

**`TooManyPins` error code:** `errorCode: 429, subCode: 16`.

Source: `§3.12`, `chunks/_app-7becf23e83ddd0c6.js:73069`. Quota limit is server-side (likely 3 for free users, unlimited + purchasable for c.ai+ via Charm packs `memo_100/200/300` at `:165431-165433`).

### 3.3 The prompt assembly position for pinned turns

From the designed prompt template in `§15.1`:

```
{{#if pinned_memos}}
Persistent memories (these are important and never expire):
{{#each pinned_memos}}
- {{this.content}}
{{/each}}
{{/if}}
```

These sit **after the character definition** and **before the dialog history**. They have `truncation_priority=0` and are thus never evicted by the truncation algorithm.

---

## 4. Sticky-session routing per chat_id

**Source:** `CHARACTER_AI.md §C3.4`, from `research.character.ai/optimizing-inference/` (recovered from archive).

> "At a fleet level, we use sticky sessions to route the queries from the same dialogue to the same server. Since our KV cache size is small, each server can cache thousands of dialogues concurrently. Our system achieves a 95% cache rate, further reducing inference cost."

The mechanism: Layer-4 sticky sessions at the load balancer (GCP LB + Envoy session affinity on `chat_id` header / cookie). All turns for `chat_id=X` land on server `Y`. Server `Y` holds the host-RAM radix-tree KV cache warm for chat X, so the prefix KV for the full conversation is never re-prefilled. **95% cache hit rate** in production.

The KV cache itself:
- Lives in **CPU/host RAM** (not GPU HBM) — 10-100× cheaper per byte.
- Indexed by **rolling hash of prefix tokens** (Rabin-Karp style — can extend incrementally as tokens are appended, O(n) total hash computation for all prefix lengths).
- Organized as a **radix tree with LRU eviction** at the leaf (dialogue branch) level; inner nodes survive while any descendant leaf is alive.
- Popular character persona prefixes (up to 32K chars / ~8K tokens) effectively never evict because they always have live leaf branches.

Estimated per-dialogue host-RAM cost: ~30-40 MB. At "thousands of dialogues per server" = ~175 GB host RAM per inference server dedicated to KV cache (matches GCP a3-highgpu-8g at 1.8 TB RAM). Source: `§C3.4` (INFERRED from blog claim + parameter math).

---

## 5. Additional signal: model routing + wire protocol

**Source:** `CHARACTER_AI.md §3.9`, `§3.2`.

**15 internal model routing slots:** `MODEL_TYPE_FAST`, `MODEL_TYPE_SMART`, `MODEL_TYPE_BALANCED`, `MODEL_TYPE_DYNAMIC` (server-side auto-router), `MODEL_TYPE_DEEP_SYNTH` (internal codename "RatGPT"), `MODEL_TYPE_DEEP_SYNTH_LITE` (internal codename "Clawd"), etc. Source: `chunks/_app-7becf23e83ddd0c6.js:73168-73188`.

The external API collapses this to **two choices:** `default` and `safe`. Source: `character-ai/openapi.yaml`, `§C3.6`. The complexity is infrastructure, not API surface.

**`MODEL_TYPE_DYNAMIC`:** "Pick the best for me" — this is the server-side model auto-router. The footer string says: "The actual model used may vary based on system load and availability." (`chunks/_app-7becf23e83ddd0c6.js:27336`). Models can silently downgrade under load.

**Unified error envelope (WS + HTTP):**

```json
{
  "command": "neo_error",
  "request_id": "...",
  "comment": "...",
  "error_code": 4xx,
  "sub_code": null,
  "retry_after_seconds": null
}
```

Source: `§3.2`, `§C3.6` (live probe). Same envelope for HTTP and WebSocket. The `retry_after_seconds` field is the rate-limit slot (matches Lancelot's `error taxonomy ok/quota/timeout` in `lancelot.upstream_metrics`).

---

## 6. Depreciating / hype items (flag explicitly)

- **The specific model codenames** (RatGPT/DeepSynth, Clawd/DeepSynthLite, c1.x lineage) are on a ~12-18 month churn cycle. Character.AI is now SFT-on-DeepSeek-V3 post-Google deal (post-deal trajectory: open-source base + proprietary post-training). Don't anchor to these names.

- **The exact truncation_step value** Character.AI uses in production is NOT in the open-source code (the `cai_helpers.py` `pretruncate_messages` uses `token_limit // 10` but that's the coarse pass, not the fine-grained step). The `truncation_step` knob is the durable primitive — the specific value they use internally is inference-only.

- **The JudgeJudy judge weights (0.4/0.4/0.2)** — these will churn as model capabilities shift. The durable primitive is the pattern: 3-judge panel, cross-validate against different model families, calibrate against human scoring, 0.7 threshold. The specific weights are operationally set.

- **Stripe price IDs** (`price_1N4XvbK6PNmzstE2MpOstqpU` etc.) — live keys captured, but they rotate on pricing changes. Not load-bearing for architecture.

---

## 7. Byte-path implications for Lancelot

| Primitive | Lancelot byte-path status |
|---|---|
| **Prompt Poet `truncation_step` cache-stable truncation** | `lancelot.canonical.dumps_canonical` provides **prompt-/KV-cache prefix stability** for serialization. BUT the `truncation_step` rounding mechanism is in the *context-assembly* layer (not yet built). Lancelot owns transport; this is agent-layer context engineering. It must be built in the Lancelot agent layer and is NOT provided by the existing byte path. |
| **`truncation_priority` per-message eviction order** | Same: agent context-assembly layer. Lancelot's `MemoryPool` handles byte-level OOM, not message-level truncation order. Not provided. |
| **Sticky-session routing per chat_id** | `Response.set_session_affinity(key)` already provides KV-cache-aware routing. The chat_id → server mapping is exactly the byte-path primitive this was built for. **Already provided.** The inference-server-side host-RAM radix cache is outside Lancelot's scope (user's inference infra). |
| **JudgeJudy 3-judge panel** | `lancelot.upstream_metrics` + `TransportPool.fanout(first_n, deadline, cancel_laggards)` can fan out to N judge providers in parallel. The eval harness / rubric / calibration are not built in Lancelot (agent-layer concern). `fanout` removes the transport cost of multi-judge; the harness still needs building. |
| **`is_pinned` + prompt injection** | This is context-assembly in the agent layer. `lancelot.mcp.mount` minimal-return discipline (>4KB WARN) is analogous (both are "what do you inject into context"), but the actual pin-to-prompt injection is not a Lancelot primitive. Not provided. |
| **`retry_after_seconds` in error envelope** | `lancelot.upstream_metrics` error taxonomy includes `quota/timeout/transient`; `TransportPool` honors `Retry-After`. The exact `retry_after_seconds` field from the WS envelope is already modeled. **Already provided.** |
| **TTFT (time-to-first-chunk) metric** | `lancelot.http.stream.ttft` is already built. Character.AI targets < 200ms TTFT on warm cache, < 1s on cold prefill — Lancelot's metric already captures this. **Already provided.** |

---

## 8. API shape notes worth stealing

- **`truncation_step` as a first-class config parameter** — not a derived constant. Expose it as an operator knob: `PromptContext(token_limit=8192, truncation_step=1000)`. The step size is a business decision (cache stability vs context fit).

- **Priority as eviction order (higher int = evicted first), not retention order** — counterintuitive but correct: 0 = "sacred", higher = "disposable". Document this clearly; the inversion bites operators.

- **`is_pinned: bool` on every message struct** — not a separate "pins" list. This makes it trivial to upgrade any message to permanent context without changing the list shape.

- **Three-level error taxonomy on the wire:** `error_code` (HTTP-aligned integer) + `sub_code` (domain-specific discrimination) + `retry_after_seconds` (rate-limit slot). All three nullable. This is the pattern for Lancelot's streaming error events.

- **`pretruncate` coarse filter before fine-grained truncation** — two-pass approach: O(1) message-count pass first, then O(n) token-level pass. The coarse pass uses `token_limit // 10` as the chunk size, which is a reasonable first-cut heuristic. Apply this in the Lancelot context-assembly layer to avoid expensive tokenization on obviously-too-long histories.
