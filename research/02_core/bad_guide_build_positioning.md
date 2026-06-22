# BAD_GUIDE — Build & Positioning Distillation
*For Lancelot agent-framework harvest. Mechanism-level only. Every claim cited.*

---

## 1. THE CONTRARIAN-SECRET STRUCTURE

**Mechanism** (BAD_GUIDE §The Bet, §Founder Mind, §YC/a16z):
Real opportunity = **conventional wisdom + contrarian truth + tech enabler that makes it doable NOW**. Most people stop at the contrarian truth. The third part — "mature enough for production, recent enough that incumbents haven't adopted it" — is where theses die.

Application to Lancelot:
- Conventional wisdom: "FastAPI is good enough for AI backends; the bottleneck is model quality."
- Contrarian truth: "Under concurrency on a single worker, your framework IS the bottleneck — GIL, serialization, streaming backpressure, KV-cache instability all owned by the framework layer."
- Enabler: pyo3 + hyper 1 + Rust async is production-stable NOW; PyO3 async was not usable at this fidelity 2 years ago.

**The test** (§Founder Mind): "If your insight leads to the same architecture as incumbents, it isn't contrarian enough." FastAPI forks are the same architecture. Lancelot owns the byte path under the framework — that is structurally different.

---

## 2. PRIMITIVES-NOT-WRAPPERS (the core product decision)

**Mechanism** (BAD_GUIDE §Building Revolutionary Products):
> "Build the primitive; the product is its consequence. Figma spent ~4 years on a WebGL renderer and a multiplayer protocol before the design tool existed."

Primitive test: state it in one sentence. "Rust-backed byte-path HTTP transport that gives Python AI frameworks 2-3× serialization throughput, sub-200ms TTFT SLA, and streaming backpressure with client-disconnect cancellation" is a one-sentence primitive.

**Wrapper test** (§Building Revolutionary Products):
> "Swap the underlying model out in your head. If the product is basically the same, you built a wrapper."

For Lancelot, swap FastAPI ↔ Lancelot: the byte-path wins (const tier, large-payload serialization, streaming TTFT, @app.batch 12×/16×) are architectural properties no FastAPI swap can match. The wrapper test passes.

**Own the quality ceiling** (§Building Revolutionary Products):
> "If your core primitive depends on a third-party service, that service controls how good you're allowed to be. Perplexity started on the Bing API, hit the ceiling, and built its own 200B-URL index."

Lancelot already lives this: owning the Rust HTTP layer is the moat. The guide explicitly names this: "For Lancelot, you already live this — owning the Rust HTTP layer *is* the moat PyO3 wrappers can't copy."

---

## 3. SCOPE REDUCTION IS QUALITY (the refusal model)

**Mechanism** (BAD_GUIDE §Product Taste):
> "Product strategy is a principled refusal model. Des Traynor's 2013 line: every feature you add costs you forever. Saying no is a product capability, not a scheduling constraint. Jobs cut Apple from 350 products to 10."

The guide's named examples of what to refuse: "ten plausible integrations that would each split your attention and dull the core loop." The cardinal rule maps directly: **No bullshit, no useless logic. Every change must change the byte path or the operator experience.**

**Harness-thinness is a positioning** (BAD_GUIDE §Building Revolutionary Products, citing Boris Cherny, Claude Code):
> "A model-forward harness that gains capability when the model does, instead of a thick scaffold that caps it."

Lancelot's 4KB MCP warn-on-return discipline, minimal-return tool doctrine (Dex Horthy pattern), and the Tier-D DO-NOT-BUILD list all instantiate this: *every abstraction is a liability*. The guide cites browser-use:
> "Every abstraction is a liability. Start with maximal capability then restrict, delete the click/type/scroll abstraction, hand the model raw DevTools Protocol."

**Codex head (Sottiaux) explicitly names this** (§CTO section):
> "Delete the harness so a smarter model's capability overhang is not capped by your plumbing."

---

## 4. OPINIONATED DEFAULTS AS MOAT

**Mechanism** (BAD_GUIDE §Product Taste, §Design):
> "Fewer, more opinionated primitives produce more coherence than infinite flexibility. Your design system should remove options, not add them."
> 
> "Linear's redesign collapsed 98 color variables down to 3."

Lancelot's specific instantiation of this:
- `LANCELOT_STREAM_BUFFER=256` (the throughput knee, clamped [1,4096]) — opinionated default with an escape hatch
- `LANCELOT_HEADER_TIMEOUT_MS=15000` (slow-loris default)
- `LANCELOT_MAX_CONNECTIONS=1024` (conn-cap default)
- `max_response_bytes=4096` on `lancelot.mcp.mount` — the 4KB minimal-return default

These are not just tunable knobs. They encode expertise: "we ran adversarial load tests; this is the production-safe number." That expertise embodied in a constant is a moat a wrapper cannot copy.

**KV-cache prefix-stability as an opinionated default** (BAD_GUIDE §CTO):
> "Keep a static system-prompt prefix, append-only context, deterministic JSON serialization, and route sessions to consistent workers. These are architecture decisions with 10x cost impact that most teams never make."

Lancelot's `lancelot.canonical.dumps_canonical` + `Response.set_session_affinity` directly embody this. The guide validates these as non-trivial: "most teams never make" these decisions. Ship them as first-class primitives with docs that say "this is how you cut your LLM bill by 10×."

---

## 5. WHAT TO REFUSE (the cost-effectiveness positioning)

**The cost story ceiling** (BAD_GUIDE §The Bet, §Product Taste):
> "Sell AI that drives revenue (no ceiling), not just cuts cost (ceiling = the headcount you displaced, then you're done)."

Applied to Lancelot's positioning: do NOT lead with "we're cheaper to run." Lead with "we unlock the AI-serving shapes (TTFT SLA, streaming backpressure, @app.batch) that let your agent product meet its SLA." The revenue story is "your AI agent can charge for reliability guarantees because the transport layer actually provides them."

**What the guide says to refuse building** (summarized from §CTO "scaffolding is coping", §Building Revolutionary Products "bitter lesson of agent frameworks"):
1. Thick scaffolding that models will outgrow (Sottiaux: delete it)
2. Abstractions over tool protocols that cost flexibility (browser-use: raw DevTools beats abstracted click/type/scroll)
3. Fine-tuning as a moat (§Research Taste: "next base release erases your edge"; harnesses age better than weights)
4. LLM serving infrastructure where Google can give the layer away free (Fal.ai: refused LLM serving, won diffusion inference)
5. Per-outcome pricing that caps at headcount displaced

**Specific refusals that map to Lancelot's Tier-D list:**
- Don't optimize `@app.stream` per-chunk overhead (upstream-rate-limited; framework is not the bottleneck)
- Don't deploy 1-process-many-threads (GIL anti-scales; multiprocess only)
- Don't claim blanket "Rust = faster" (honest perf envelope; specific wins only)

---

## 6. COMPOSITE ARCHITECTURE BEATS BOTH WRAPPERS AND FINE-TUNING

**The v0 pattern** (BAD_GUIDE §Building Revolutionary Products):
> "Vercel's v0 took Claude Sonnet's 64.71% error-free rate to 93.87% with engineering, not model training: RAG context injection, real-time stream-fixing during generation, deterministic AST fixes, a fine-tuned AutoFixer."

The guide's key point:
> "Malte Ubl's rule: don't train your own foundation models if you want to iterate fast; use a composite architecture to stay flexible. The compounding moat is the harness around the model, not the model itself. It survives every frontier release instead of being obsoleted by it."

Lancelot's agent layer should follow the same pattern: composable byte-path primitives (TransportPool, canonical, TTFT, batch) + the user's model, NOT a vertically integrated model wrapper. The user keeps the model; Lancelot compounds on the transport.

**Panel-of-models beats single frontier model** (BAD_GUIDE §CTO, citing OpenRouter Fusion):
> "A budget panel (Gemini 3 Flash + Kimi K2.6 + DeepSeek V4 Pro) beats solo GPT-5.5 and Opus 4.8 and lands within 1% of Fable 5 at roughly half the price, with ~¾ of the lift coming from synthesis and only ~¼ from diversity."

This validates `TransportPool.fanout(first_n=, cancel_laggards=True)` as a primitive that directly implements the winning architecture. The harness wins, not the weights.

---

## 7. B2B GTM: COST-EFFECTIVENESS POSITIONING

**The value stack mechanism** (BAD_GUIDE §B2B GTM):
> "Hormozi's Grand Slam Offer: stack so much value that the price becomes a rounding error against the outcome. His Value Equation: (dream outcome × perceived likelihood) / (time delay × effort)."

Lancelot's positioning levers from this equation:
- **Dream outcome**: "your AI API meets its TTFT SLA under concurrency"
- **Perceived likelihood**: "validated under adversarial oha load (not just TestClient)"
- **Time delay**: "10-minute `pip install lancelot` swap; no config"
- **Effort**: "zero-config const tier, one decorator @app.batch"

**Outcome pricing, not seat pricing** (§B2B GTM, citing Palantir/McGrew):
> "If you save a customer $2M, charge $500K. Outcome pricing forces conviction on both sides."

For Lancelot: the measureable outcomes are (a) LLM cost reduction via KV-cache prefix stability + @app.batch, (b) TTFT p99 improvement, (c) uptime improvement via graceful drain + circuit isolation. These are instrumentable. Quote numbers from the honest benchmark.

**Language/market fit before features** (§B2B GTM, citing Matt Lerner):
> "Customers convert when your copy sounds like the sentence already in their head. Test the words before you build features. Lerner moved conversion from 0.5% to 40% on copy alone."

The sentence in the target buyer's head (AI-backend engineer at a startup): "I need my streaming to not break under load and I need my LLM bill to stop scaling with traffic." Lancelot's messaging should be exactly those words, not "Rust HTTP framework."

---

## 8. DISTRIBUTION: LANCELOT'S SPECIFIC CHANNEL SIGNAL

**The guide's framing** (§Distribution, verified 7 cases: Cursor, Perplexity, Midjourney, ElevenLabs, v0, Bolt.new, Lovable):
> "None of them [got their first 1,000 users via] paid acquisition. They launched into a crowd they already had."

**Frictionless entry beat any campaign**: Cursor shipped a free no-credit-card VS Code fork; Bolt.new matched 7 years of StackBlitz ARR in 48 hours off one tweet with zero signup.

**The shareable demo moment**: this is what drives the spike. "Look what the bench numbers say" → thread on X showing @app.batch 12× with a real oha trace is the shareable moment.

**Personal Monopoly** (§Leverage):
> "Own a unique crossing of topics instead of competing on a crowded one. 'Rust HTTP performance + Claude-Agent-SDK ops + Central Asian operator' is a lane with one occupant."

---

## 9. HONESTY FLAGS (depreciating vs durable)

**Durable** (survives 3+ model generations):
- Byte-path transport ownership (hyper/tokio substrate doesn't change when GPT-7 ships)
- KV-cache prefix stability (`dumps_canonical`) — structural, not tactical
- Backpressure + client-disconnect cancellation — structural TCP reality
- Opinionated production defaults (the knobs encoding load-test findings)
- Graceful drain + RED metrics — cloud-native fundamentals

**Depreciating** (churns on ~1-2yr model clock):
- `@app.batch` 12× numbers — valid TODAY against rate-limited APIs; on-prem models may not have this bottleneck shape in 2 years
- TTFT absolute numbers — 200ms cliff is valid now; model inference may get 10× cheaper making this a solved problem at the infra layer
- Specific provider shapes in `lancelot.upstream_metrics` (OpenAI/Anthropic/Google token field paths) — churn with every API update

**The guide's specific warning** (§Research Taste):
> "Fine-tuning is usually a capital trap. Spend millions fine-tuning a model and the next base release erases your edge. Model-agnostic harnesses age better."

This validates Lancelot's no-model-dependency posture as the durable bet.

---

## 10. AGENT HARNESS PRINCIPLES WITH BYTE-PATH IMPLICATIONS

**Context-as-finite-resource** (BAD_GUIDE §Building Revolutionary Products, Anthropic Applied AI):
> "Context is a finite resource to curate: degradation is a gradient from n-squared attention, not a cliff, and just-in-time retrieval beats pre-loading the corpus."

Lancelot byte-path implication: `lancelot.mcp.mount` 4KB warn-on-return is the practical enforcement of this. When a tool returns >4KB, it is probably loading context rather than triggering action. This is the right default — flag it, let the operator decide.

**Feature-list-as-executable-spec** (BAD_GUIDE §CTO, Anthropic "Effective harnesses for long-running agents"):
> "A feature-list-as-executable-spec the agent may toggle but not edit, git-as-state-machine for recovery, a progress file as memory across context windows."

Lancelot lacks: the durable state seam (event-sourced checkpoint/resume). `lancelot.context` ContextVars are request-scoped, not durable. This is a confirmed gap (see 00_LENS.md §What Lancelot LACKS).

**Decouple decider from executor** (BAD_GUIDE §CTO):
> "Cursor's apply model: a big smart model writes a sketch with placeholders, a small fast fine-tuned model performs the actual edit at ~1,000 tok/s (13× faster) because the existing code is its draft tokens."

Lancelot byte-path implication: `TransportPool.fanout` + `spawn_child` already support the multi-model topology (orchestrator → worker). The byte-path is ready; the agent loop layer needs to expose "orchestrator model" vs "executor model" as a first-class config.

**Single-threaded context is the winning agent architecture** (BAD_GUIDE §Building Revolutionary Products, Cognition/Walden Yan):
> "Don't Build Multi-Agents: share the full agent trace, not only the messages, and accept that parallel agents make conflicting implicit decisions that produce broken output."

Lancelot byte-path implication: `spawn_child` propagates traceparent + request_id, which is the single-trace requirement. The architecture is sound; the agent loop layer should make single-trace context sharing the default, not an opt-in.

---

## SUMMARY: The 5 Mechanism-Level Takeaways for Lancelot

1. **Primitive in one sentence**: "Python-first HTTP transport that gives AI backends 2-3× serialization throughput, bounded streaming with TTFT, and zero-GIL const tier for health probes — all validated under adversarial oha load."

2. **The wrapper test passes**: swap FastAPI ↔ Lancelot and the byte-path wins survive; swap LLM providers and nothing breaks. Moat is structural.

3. **Opinionated constants encode expertise**: `LANCELOT_STREAM_BUFFER=256`, `max_response_bytes=4096`, `LANCELOT_HEADER_TIMEOUT_MS=15000` are not just defaults — they are the load-test findings crystallized into config. Document them that way.

4. **Revenue story, not cost story**: "TTFT SLA under concurrency → your users don't churn at 500ms latency → you can charge for reliability guarantees." Not "we're cheaper."

5. **Refuse the agent-logic layer**: the guide (Cognition, Anthropic, Sottiaux) all confirm the harness compounds across model generations but AI-logic bets (fine-tuning, orchestration topologies, prompt templates) get reset by each model release. Stay byte-path.
