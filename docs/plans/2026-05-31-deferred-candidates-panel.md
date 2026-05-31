# Deferred-candidate usefulness — 5-judge panel verdict

> **Date:** 2026-05-31 · **Decision artifact** (not code). 5 unbiased judges (AI-startup builder / no-bullshit maintainer / ecosystem-already-solved / positioning-differentiation / evidence-recurrence-skeptic) independently rated the 9 deferred candidates from `docs/plans/2026-05-31-fastapi-rust-absorb-mine.md`, then combined. Each cross-checked against live source + the 24-project corpus.

---

# Which deferred candidates are actually worth building — 5-judge panel

> **Verdict basis:** 5 independent judges (AI-startup builder / no-bullshit maintainer / ecosystem-already-solved / positioning-differentiation / evidence-recurrence-skeptic), cross-checked against the live Lancelot source and the 24-project corpus. **Load-bearing context every panel surfaced and I verified:** the evidence doc (dated 2026-05-31) is the *source* of the M16 plan, and **P1 (defer_status), P2 (max_stream_silence + keepalive self-reset fix), and P8 (pool.stream `request=` + pool.relay) ALREADY SHIPPED** (commits c51fe92, fd56f0c — confirmed in `git log` and in `llm.py:482/539/568`, `asgi.py:945/1043`). They are not in the 9 judged here, but their shipped status **shrinks P6** (now a polish delta on shipped machinery) and **sharpens P7** (the genuine remaining streaming-observability gap). Also verified architectural: the native `app.run()` server forwards only routes (`app.py:877`) → **no Rust pre-handler seam**, so P3/P5/R1 must live on the Python ASGI seam (stdlib hmac/zlib are already C-fast there). That kills the "zero-GIL gate" framing the original mine flirted with.

---

## 1. Consensus table (best → worst)

| Candidate | 5 calls tallied | Consensus | Dominant label | Who actually needs it |
|---|---|---|---|---|
| **P7 — streamed cost/token/TTFT accounting** | 5 BUILD | **BUILD** | useful (parity-bug fix) | Any AI startup billing/dashboarding per-token cost on **streamed** completions through `pool.stream`/`relay` — today it silently records 0 while unary records correctly. |
| **P3 — inbound rate-limiter → 429 + Retry-After** | 4 BUILD / 1 MAYBE | **BUILD** | useful | Anyone exposing public `/chat` or `/login` on single-container PaaS — exactly Lancelot's deploy target; `fastapi-users` itself flags "no rate limiting on /login". |
| **P5 — constant-time static-token gate** | 3 BUILD / 2 MAYBE | **BUILD** | useful (get-it-right-once) | Operators locking a vLLM/Ollama-style inference endpoint behind a static bearer token without standing up a JWT framework. |
| **P4 — response_model skip-validate** | 3 BUILD / 2 MAYBE | **BUILD (fenced)** | useful, but footgun | Teams with hot list/read routes over a trusted ORM who vouch for the shape. The 4× lands on multi-row reads. |
| **P6 — guaranteed SSE terminal + crash/cancel→error-frame** | 3 BUILD / 2 MAYBE | **BUILD (as polish, bundle w/ P7)** | useful, narrow delta | Every SSE/token-streaming client that must tell a crashed/cancelled stream apart from a complete one. |
| **P9 — Page[T] envelope + safe pagination/sort** | 1 BUILD / 3 MAYBE / 1 SKIP | **RE-SCOPE → BUILD the DoS-cap, SKIP the envelope** | split | DoS-cap half: any DB read API taking client-controlled page size. Envelope half: nobody — 5-7 teams each ship their own shape. |
| **R1 — GIL-releasing gzip (Rust)** | 4 MAYBE / 1 SKIP-leaning | **MAYBE — bench-gate + maintainer sign-off** | niche, conditional | Single-container PaaS with no compressing proxy returning large JSON under concurrency. NOT anyone behind a TLS proxy. |
| **R2 — SIMD base64 for embeddings (Rust)** | 2 SKIP / 3 MAYBE-leaning-skip | **SKIP (bench-first, expect parity)** | redundant with `pip install pybase64` | Only vLLM-class embedding servers emitting base64 float arrays — and they already vendor pybase64. |
| **R3 — cross-process model-worker broker (Rust)** | 4 SKIP / 1 SKIP(positioning) | **SKIP unless a maintainer expands the charter** | positioning-risk / overkill | Authors of model-serving frameworks (litserve/ray/modal) — i.e. the user-side "hosting the model" that the positioning lock excludes. |

---

## 2. Per-candidate verdict (ordered by consensus strength)

### P7 — Streamed-response cost/token/TTFT accounting → **BUILD** (unanimous, all 5)
**Strongest FOR:** This isn't a feature; it's a **silent-wrong-number parity bug** on the observability pillar. Verified: `pool.request` records cost via `_emit_upstream` → accumulates into the `cost_usd` ContextVar (`llm.py:394`/`:444`), but `pool.stream` (`llm.py:482`) and `_relay_forward` (`:583`) just forward bytes and call **nothing** — so the `X-Lancelot-Cost-USD` trailer and `upstream.*` metrics read 0 for every streamed LLM call. The fix reuses `extract_tokens`' existing 3 provider shapes (`upstream_metrics.py:108`), scanning the terminal SSE frame instead of a parsed dict — **no tiktoken dep**, stays pydantic-only. r2r literally re-runs tiktoken to recover the number the stream lost.
**Strongest AGAINST:** AI-startup judge ranks it one notch below P3-P6 because *"it pays off only if I adopt TransportPool for my upstream calls"* (direct SDK callers don't benefit). The positioning judge pre-empted the only real objection: it looks like AI-metering scope-creep, but it **closes a parity hole with the already-shipped unary cost surface** — provider-reported-usage-only keeps it observability, not AI-logic.
**Disagreement:** None on the call. Only on ranking (AI-startup: "step below"; everyone else: highest-coherence build).
**Recommendation:** Build it opt-in (`track_usage=True` so you don't frame-scan every relay). The most clearly-correct item on the board.

### P3 — Inbound rate-limiter → 429 + Retry-After → **BUILD** (4 BUILD / 1 MAYBE)
**Strongest FOR:** Genuinely absent inbound — verified every `429`/`Retry-After` in the tree is OUTBOUND (`llm.py`), the batch shed, or the conn cap; `tenant.py` is per-tenant **concurrency** (`asyncio.Semaphore` at `:78`/`:98`), not rate-over-window. It's a resilience **cap** — same family as the body/conn/timeout caps Lancelot already prides itself on — and the `_TokenBucket` math already exists (`llm.py:128`) so the surface is small. Corpus solves it 3 incompatible ways (dispatch=slowapi, polar=Redis, r2r=hand-rolled), and `fastapi-users` flags `/login` has none. No-bullshit judge: "changes the operator experience, small surface, on-mission."
**Strongest AGAINST:** Ecosystem judge's MAYBE: **slowapi is one `pip install` away and is maintained + feature-rich**, so Lancelot wins only on *leaner* (pydantic-only, reuses the bucket) + *byte-path-integrated* (429 on the same seam as the conn-cap) — "a real but narrow edge." The shared footgun all judges name: a **per-process bucket silently under-counts on the multiprocess deploy Lancelot itself mandates**.
**Disagreement:** BUILD vs MAYBE turns on whether "slowapi exists" is disqualifying. The majority say no — a resilience-first framework should own its ingress cap as a first-class primitive, *if* it stays thin.
**Recommendation:** Build as one opt-in ASGI middleware → 429 + `Retry-After` + `RateLimit-*`. Per-process default **with a loud docstring** + a pluggable backend hook for Redis. **Do NOT ship a Redis dep and do NOT grow a rule DSL** (every judge drew that line).

### P5 — Constant-time static-token / API-key gate → **BUILD** (3 BUILD / 2 MAYBE)
**Strongest FOR:** Verified `security.py` only **extracts** (`HTTPBearer` returns credentials at `:126`, OAuth2 returns `param` at `:201`) — nothing compares inbound. It's stdlib `hmac`/`secrets` only (no dep, no Rust), and the corpus proves the subtleties are a **get-it-right-once trap**: vLLM (`server_utils.py:55-83`) and Prefect both hand-roll it and **diverge** on Host-spoof (`scope['path']` vs `request.url`), OPTIONS-skip, and health-allowlist. No-bullshit judge: "auth IS resilience; the value is getting the 15 lines right once."
**Strongest AGAINST:** Two MAYBE judges note the **need is narrow** — it's the *degenerate* auth case (a shared static key). Most real apps need JWT/sessions, so "everyone needs this" collapses to "inference-endpoint operators need this." The differentiation is "we got the stdlib right for you," not a moat.
**Disagreement:** BUILD vs MAYBE is purely about audience size, not soundness. The "zero-GIL Rust gate" framing is **dead** across all panels (verified: native server runs no pre-handler seam).
**Recommendation:** Build as a thin ASGI middleware that **always reads `scope['path']`** (never `request.url`), skips OPTIONS, takes an allowlist. Rank below P3/P7. **Hard line every judge drew: do NOT let it metastasize into a JWT/JWKS/multi-scheme auth framework** — that's a separate, lower-priority, possibly-out-of-scope thing.

### P4 — response_model skip-validate → **BUILD, but tightly fenced** (3 BUILD / 2 MAYBE)
**Strongest FOR:** Verified double pass (`asgi.py` model_validate then `dump_json` — line ~1525/1533 in current source, ~1373/1381 in the pre-M16 doc). `response_model` is FastAPI's near-universal read-route norm; 4× on a 1000-row read is credible because it eliminates a whole pydantic-core traversal. Opt-in, byte-identical when off — the AI-startup and ecosystem judges call it the "cheapest high-leverage item in the set."
**Strongest AGAINST (the load-bearing catch):** The no-bullshit judge surfaced a real correctness collision I **verified in source**: Lancelot already ships `response_model_exclude_unset`/`exclude_defaults`/`by_alias` (`routing.py:84-86`, applied at `asgi.py:1535-1537`), and `exclude_unset`/`exclude_defaults` are **computable only from a validated model's `__pydantic_fields_set__`**. A skip-validation path that dumps the raw object either **silently drops those flags or lies**. The positioning judge adds: the eliminated pass is the safety net behind the marquee 99.6% byte-parity.
**Disagreement:** BUILD vs MAYBE hinges entirely on this collision. It is resolvable, not fatal.
**Recommendation:** Build it, but **gate it hard at registration**: refuse to combine `skip_validation` with any `exclude_*`/alias flag (raise at registration), so it can never silently lie. Default stays parity. Ship it bundled with response/serialization work, not as a headline.

### P6 — Guaranteed SSE terminal + crash/cancel→error-frame → **BUILD as polish, bundle with P7** (3 BUILD / 2 MAYBE)
**Strongest FOR:** No pip package fixes this — it's a correctness gap in Lancelot's **own marquee streaming**, and the most coherent thing to finish given M16 just shipped P1/P2/P8 into the same cluster. Verified the residual gaps are real post-M16: the mid-stream error frame is still a **HARDCODED `"internal server error"`** (`asgi.py:1199`), not the shipped `ProblemDetail`/`retry_taxonomy`; and the `CancelledError` arm (`:1160-1166`) does **not propagate but emits no error frame**. vLLM/litserve/Letta all hand-roll the identical finalizer. A truncated stream with no error frame and no terminal `[DONE]` lets the client mistake a crash for an EOF — the worst chat-UX failure.
**Strongest AGAINST:** Two MAYBE judges correctly downgrade it: after M16 this is a **polish/correctness delta on already-shipped machinery, not a feature**. And there's a sharp footgun: emitting an error frame on **every** `CancelledError` is wrong — a normal client disconnect is not an error, and you must NOT write to a half-gone socket (the R2 comment at `asgi.py:1225` exists precisely because send-on-broken-socket bites).
**Disagreement:** BUILD vs MAYBE is about whether to milestone it (no) or fold it in (yes).
**Recommendation:** Build as a **small consolidation, not a milestone**: route the existing frame through `ProblemDetail`, emit the terminal sentinel in `finally`, and emit the cancel-arm frame **only for a genuine internal cancel (silence-abort), never a client disconnect**. Bundle with P7's relay work. Validate on a real mid-stream raise AND a real disconnect.

### P9 — Page[T] envelope + safe pagination/sort → **RE-SCOPE: BUILD the DoS-cap, SKIP the envelope** (1 BUILD / 3 MAYBE / 1 SKIP)
**This is the candidate the panel most wants split.** Four of five judges independently said: it's two features with opposite verdicts.
- **The Page[T] envelope = SKIP / bullshit.** Verified 5-7 corpus projects each define a **different** shape (`{items,total}` vs `{data,count}` vs `{results,total_entries}` vs `{result,count,previous,next}`). That divergence is *proof they won't adopt yours* — they're committed to their shape, and Lancelot's generic envelope rots as dead surface supported forever. Ecosystem judge: `fastapi-pagination` already gives `Page[T]`/`add_pagination` off-the-shelf (langflow uses it). Zero byte-path angle — parsing an int is nanoseconds; the cost is the user's `COUNT(*)`. Building it is the no-bullshit rule's redundant-logic trap.
- **The DoS-cap = BUILD.** Verified the footgun is real and live: dispatch `database_service.py:738` `if items_per_page == -1` → unbounded full-table read by query param; bitcart `Query(ge=-1, le=1000)`. Lancelot's cap family (body/conn/timeout) has **no page-size cap**. `PageParams` with `LANCELOT_PAGE_MAX_SIZE` and **no -1 escape** + `SortParam` allowlist→422 (anti sort-injection) is a genuine resilience cap on the family Lancelot already owns.
**Disagreement:** The lone BUILD (positioning judge) explicitly says the **cap is the load-bearing differentiator and the envelope alone is commodity** — so it actually agrees with the re-scope.
**Recommendation:** Ship `PageParams` + `SortParam` as a safe-by-default pagination dependency (the DoS cap is the whole justification). **Drop the Page[T] envelope.** Never sell either as perf.

---

### The three Rust candidates (all gated — none build speculatively)

### R1 — GIL-releasing gzip → **MAYBE: bench-gate + maintainer sign-off** (4 MAYBE / 1 SKIP-leaning)
**Strongest FOR:** The mine found a genuinely new argument that reopens a settled door **legitimately**: the standing Tier-D rejection (CLAUDE.md ~190/208) rejected gzip on "the proxy does it / reimplementing Starlette = useless" and **never reasoned about the GIL**. Verified premise: Starlette's `GZipResponder` runs `gzip.GzipFile(compresslevel=9)` on the event-loop thread **holding the GIL for the whole zlib pass**, so a multi-KB body serializes every other request on that worker. A Rust `libdeflate` under `allow_threads` is a **structural concurrency win middleware can't match** — and "the proxy does it" is false on single-container PaaS, which M12 made first-class.
**Strongest AGAINST:** The AI-startup buyer (the actual customer): *"I terminate TLS at a proxy that compresses, or my hot payloads are SSE streams (excluded from gzip anyway) — I would not switch to Lancelot for this."* It's **invisible on loopback** (can't be benchmarked the obvious way), adds a Rust codec dep + a hot-path content-negotiation branch owned forever, recurrence is thin (**2 distinct projects, not 3/24** — critic-corrected), and it **reopens a documented decision**.
**Disagreement:** None on the gate. The split is whether it ever clears it.
**Recommendation:** **No bench + no sign-off → it stays rejected.** Ships ONLY if (a) a concurrency bench (c≈200, level 9, Rust-libdeflate-under-allow_threads vs `GZipMiddleware`) shows a real per-worker throughput cliff being fixed, AND (b) a maintainer signs off on reopening Tier-D.

### R2 — SIMD base64 for embeddings → **SKIP (bench-first, but expect parity → don't build)** (2 SKIP / 3 MAYBE-leaning-skip)
**Strongest FOR:** The strongest corpus evidence in the mine, verified verbatim: vLLM `import pybase64 as base64` (`chat_completion_serving.py:14`) + the literal comment *"pybase64 is ~3x faster than the stdlib base64 on large payloads thanks to SIMD"* (`:1086`). The float-array→OpenAI-wire transform is genuinely byte-path.
**Strongest AGAINST (decisive):** The ~3× is vLLM's measurement of **pybase64**, which any user gets with `pip install pybase64` **today**. So the bar is not "beat stdlib" — it's "**beat pybase64-under-allow_threads materially**," against a library that binds the same `base64`-crate SIMD family. Realistic outcome: parity. The need also collapses to **one project doing the hot thing** (litserve only declares a base64 *field type*, not a hot encode), and as an AI startup you **consume** embeddings from an API — you don't serialize float32 arrays to the wire. Verified Lancelot's own base64 use is tiny fixed strings (`security.py:153` basic-auth decode, `webhook.py:57` signatures) where stdlib is plenty — a generic codec would be useless. Cardinal rule #1 literally: "before building, check it isn't already provided — it is, by pip."
**Disagreement:** SKIP vs MAYBE is only about whether to *run* the bench. The honest default if it merely matches pybase64 is **"document pybase64 in a recipe, ship nothing."**
**Recommendation:** **Document pybase64, build nothing.** Only the narrow `encode_embeddings(vectors)` helper could ever justify it, and only on a decisive bench win that the evidence says is unlikely.

### R3 — Cross-process model-worker broker → **SKIP unless a maintainer expands the charter** (4 SKIP / 1 SKIP-positioning)
**Strongest FOR:** The one clean NEW_RUST_WIN — verified the mechanism lines up with the bottleneck: litserve's uid-demux (`openai_spec.py:514-524`, per-uid `asyncio.Event` + `response_queue_id`) documents `multiprocessing.Manager`'s per-item pickle-through-a-proxy anti-pattern, and a Rust keyed `mpsc`/`oneshot` routing `Bytes` GIL-free (one demux per streamed token) genuinely beats it. Real recurrence (litserve/ray/modal).
**Strongest AGAINST (the brightest line on the board):** Owning an inbound model-worker broker **walks straight into "hosting the model,"** which the positioning lock puts firmly user-side. The tell, named by two judges: the corpus "recurrence" is precisely the set of products that **ARE model-serving frameworks** — i.e. this competes *with* the user, not serves them. And it's a **30-50h new Rust subsystem** (per-uid registry + process-transport pump + cross-process backpressure + worker-death tests) — the worst effort:risk:scope-creep ratio in the set. The AI-startup buyer is blunt: *"if I need a model broker I reach for vLLM/Ray Serve, not my HTTP byte-path framework."*
**Disagreement:** None — five SKIPs. The merit is real; that's exactly why a future agent will be tempted to rationalize past the positioning lock.
**Recommendation:** **Do not build speculatively.** SKIP until a maintainer makes an explicit, deliberate positioning pivot — and even then it ranks **behind every cheap, in-scope win** (P3/P5/P7), never instead of them.

---

## 3. Bottom line

**BUILD (clear, on-mission, cheap, pure-Python on the ASGI seam):**
- **P7** — streamed cost/token/TTFT accounting. *Unanimous.* This is a silent-0-cost **parity bug** on the observability pillar, not a feature. Highest-coherence build. Opt-in.
- **P3** — inbound rate-limiter (429 + Retry-After). A first-class ingress **cap** in the family Lancelot already owns; reuses `_TokenBucket`. Per-process default + Redis hook, loud docstring, no DSL, no Redis dep.
- **P5** — constant-time static-token gate. Stdlib `hmac`/`secrets`, get-it-right-once (corpus impls diverge on Host-spoof). Always `scope['path']`. **Must not grow into a JWT framework.**

**BUILD WITH A FENCE (resolvable footgun — gate it or it lies):**
- **P4** — response_model skip-validate. Real 4× on list reads, but **refuse to combine with the already-shipped `exclude_unset`/`exclude_defaults`/`by_alias` flags** (verified they require a validated instance) — raise at registration. Default parity.
- **P6** — guaranteed SSE terminal/error-frame. After M16 this is a **polish delta**, not a milestone — bundle with P7. Route the hardcoded `"internal server error"` (verified at `asgi.py:1199`) through `ProblemDetail`, emit the terminal sentinel in `finally`, and emit the cancel-arm frame **only for a genuine internal cancel, never a client disconnect**.

**RE-SCOPE (ship the cheap useful half, drop the bullshit half):**
- **P9** — **BUILD `PageParams`+`SortParam` (the DoS-cap)**, verified-real footgun (`limit=-1`→unbounded read in dispatch `:738` + bitcart). **SKIP the `Page[T]` envelope** — 5-7 teams each ship a different shape, so nobody adopts yours; `fastapi-pagination` already exists; zero byte-path angle. The envelope is gold-plating that rots.

**MAYBE — bench-first + sign-off (do NOT write a line until then):**
- **R1** — GIL-releasing gzip. The GIL argument is legitimately new (verified: Starlette holds the GIL across level-9 zlib) and reopens Tier-D *honestly*. But it's invisible on loopback, narrow (proxy-less PaaS only, 2 projects), and reopens a documented decision. **No concurrency-bench-proving-the-cliff + no maintainer sign-off → stays rejected.**

**SKIP (and why no one needs them):**
- **R2** — SIMD base64. **`pip install pybase64` already gives users the exact ~3× vLLM measured.** A Lancelot binding must beat pybase64-under-allow_threads (same SIMD family) — realistically parity. Need collapses to one inference-server shape; AI startups *consume* embeddings, don't serialize them. **Document pybase64, build nothing.**
- **R3** — cross-process model broker. The Rust win is real, but owning an inbound model-worker broker **is "hosting the model"** — the brightest line in the positioning lock. The corpus "demand" comes from model-serving frameworks (the user, not the customer). 30-50h with the worst risk:scope ratio on the board. **Do not build without an explicit charter expansion, and never before the cheap in-scope wins.**

**Where I sided against a judge:** On **P9**, one judge voted full BUILD and one voted full SKIP — I side with the **three re-scopers and the evidence**: the verified DoS footgun justifies the cap, the verified 5-7-way envelope divergence kills the envelope. On **R2**, where the mine's own grade was NEEDS_BENCH and one judge kept it as MAYBE, I side with the two SKIP judges and the `pip install pybase64` evidence — running the bench is fine, but the honest expected outcome is parity, which means ship nothing. The single biggest cross-cutting correction: **the doc is stale — P1/P2/P8 already shipped (M16)**, which is exactly why P7 (not P6) is the real remaining streaming gap, and why P6 is a bundle-in, not a milestone.