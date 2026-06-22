# Microsoft + Google Agent Patterns — Mechanism-Level Harvest
*Sources: TRANSCRIPTS_MICROSOFT.md · TRANSCRIPTS_GOOGLE.md | Mined 2026-06-22*

---

## Orientation: what these sources are

Eight Microsoft Developer videos (AutoGen / Semantic Kernel / Azure AI Foundry / MCP co-creator / CUA / open standards) and eight Google Developer videos (ADK / A2A / Pipecat / Gemini serving / agent security). Combined they represent big-tech production engineers and framework authors speaking at the mechanism level — not marketing. The signal-to-noise ratio is high in the security, observability, and multi-agent orchestration sections; low in the "agents are digital employees" framing sections.

---

## 1. Durable primitives (model-clock-independent)

### 1.1 Agent identity scoping — token TTL = task lifetime

**Mechanism:** OAuth token valid for "exactly the time it needs to execute, then gone." The token request is itself an authorization checkpoint: "are they on a mission that's actually authorized?" Microsoft's Entra ID introducing agent-specific token lifecycle management.

**Why durable:** this is a least-privilege enforcement pattern that predates LLMs. The specific API (Entra) is tactical, but the *shape* — token scoped to task + zombie-token prevention — is permanent.

**Lancelot byte-path:** `lancelot.context` already propagates `agent_id` + `tenant_id` as ContextVars. The *token lifecycle* enforcement is NOT done — Lancelot issues no OAuth tokens. An agent framework would sit above Lancelot; Lancelot's seam is: `agent_id` from the auth token → ContextVar → emitted in every OTel span.

*Source: TRANSCRIPTS_MICROSOFT.md §"Identity as the Foundation for Everything"*

---

### 1.2 Output redaction vs. output blocking — SDP pattern

**Mechanism (Google Model Armor):** sensitive data protection (SDP) **redacts** PII from tool responses before they reach the model — it does NOT block (blocking breaks application flow). The model sees `[REDACTED]` in place of SSN/CC numbers. The redacted form also appears in logs so engineers can review agent performance without seeing real customer data.

Flow:
```
tool_response → SDP.redact() → model → model_response → SDP.redact() → client
```

**Why durable:** redact-vs-block is the correct architecture for any data leakage guard in an LLM pipeline. Blocking on PII-in-tool-responses creates endless false-positive failures; redaction preserves flow.

**Lancelot byte-path:** `lancelot.net` has SSRF guard (blocks private IPs). PII redaction in tool responses is NOT currently in Lancelot. This belongs at the agent-layer tool executor, not at the transport layer. The streaming seam (`@app.stream`) is the right interception point for output SDP — a transform stage that runs before the first chunk hits the wire. Lancelot's TTFT metric (`lancelot.http.stream.ttft`) would still trigger after the first non-redacted chunk, which is correct.

*Source: TRANSCRIPTS_GOOGLE.md §"Output Filtering: Sensitive Data Protection (SDP)"*

---

### 1.3 Authentication inside the tool, not the agent

**Mechanism:** the agent passes a session ID to the tool; the **tool** exchanges the session ID for an access token with the IDP — credentials are never visible to the LLM context. The user never sees tokens. Pattern:

```
client → agent (session_id only) → tool (session_id) 
→ IDP (exchange session_id → access_token)
→ API (access_token)
→ tool (response)
→ agent (response, no credentials)
```

**Why durable:** this is the standard OAuth service-account pattern adapted to the agent layer. Structurally prevents the LLM from ever seeing a credential it could leak.

**Lancelot byte-path:** `TransportPool` auto-injects `x-request-id`+`agent_id`+`traceparent` — but carries NO credentials. The tool execution layer (currently `@app.tool` / `lancelot.mcp`) does not enforce credential-inside-tool isolation. This is an agent-framework concern, not a transport concern. Worth encoding as a design principle in `lancelot.tool` documentation.

*Source: TRANSCRIPTS_GOOGLE.md §"Authentication in AI Agents: Service-to-Service Pattern"*

---

### 1.4 `before_model_callback` hook — the injection fence

**Mechanism (ADK):** a synchronous guard called with `(ctx, prompt)` before every model invocation. If the guard raises, the request is blocked — the model never sees the input. Google's `before_model_callback` example:

```python
def before_model_callback(ctx, prompt):
    response = model_armor.inspect(prompt)
    if response.match_found:
        raise BlockedRequestException(response.match_type)
    return prompt
```

This is the ADK hook for prompt injection / jailbreak defense. The inspection uses a *specialized* evaluator (Google Model Armor), NOT the same LLM — "AI is not always the answer to AI security."

**Why durable:** the hook shape (intercept-before-model, typed exception for block) is the right place for input safety regardless of which evaluator sits behind it. The evaluator itself (Model Armor, a fine-tuned classifier, a regex battery) is tactical.

**Lancelot byte-path:** Lancelot has no `before_model_callback` hook concept today. The closest analogue is a `@app.middleware("http")` intercept, but that intercepts HTTP requests, not agent turns. An agent loop would need its own `before_model` / `after_tool` / `before_emit` hook chain. This is a build-target for the agent layer.

*Source: TRANSCRIPTS_GOOGLE.md §"Model Armor: Input Filtering Architecture"*

---

### 1.5 Evaluator metrics: four specific axes

Microsoft Azure AI Foundry's evaluation framework names four precise metrics — not vibes, named in the SDK (`azure.ai.evaluation`):

| Metric | What it catches |
|---|---|
| `RelevanceEvaluator` | Agent "went down a different path" — interpreted task incorrectly |
| `GroundednessEvaluator` | Response asserts facts with no tool output to back them up |
| `ToolAccuracyEvaluator` | Agent called the wrong tool, or has too many similar tools causing confusion |
| `ToolOutputUtilizationEvaluator` | Tool was called, output was received, but agent IGNORED it |

The last two are orthogonal — `ToolAccuracyEvaluator` catches mis-routing; `ToolOutputUtilizationEvaluator` catches "called the right tool, then hallucinated anyway."

**Why durable:** these four dimensions map to the four ways a tool-calling agent fails. They're model-agnostic failure modes, not evaluation tactics.

**Lancelot byte-path:** Lancelot has no eval primitives today. The Azure Foundry SDK handles storage + running evals — but the *metric taxonomy* above is worth building into a Lancelot-native eval module. `GroundednessEvaluator` especially: easy to implement as a judge prompt that checks whether every assertion in the response appears in `tool_outputs`.

*Source: TRANSCRIPTS_MICROSOFT.md §"Evaluation Framework: Four Metrics"*

---

### 1.6 Agent-scope OpenTelemetry — Microsoft's PR to OTel community

**Mechanism:** Microsoft created an "agent scope" — a specific telemetry schema for agent spans — and submitted it as a pull request to the OpenTelemetry community. This defines new span attributes for agent orchestration events: start/invoke/resume/escalate/audit. Not yet adopted industry-wide.

The **specific claim:** Lancelot already emits OpenTelemetry (`lancelot.upstream.*`, `lancelot.http.*` histograms). The agent scope adds span-level attributes for: which agent handled the turn, which tool was called, what the plan was, what the decision was.

**Why durable:** if OTel adopts the agent scope, every observability backend (Splunk, Datadog, Grafana) will understand it without custom schemas. Build to the OTel shape now to get free integration later.

**Lancelot byte-path:** Lancelot already holds `agent_id` in `lancelot.context`. The OTel emissions in `record_request` only emit HTTP spans. An agent loop's OTel adapter should emit `agent_span` attributes (turn_id, tool_name, plan_text, decision_text) INTO the existing trace so they're correlated with the HTTP span via `traceparent`. Already possible today — just needs the attribute keys from the Microsoft PR.

*Source: TRANSCRIPTS_MICROSOFT.md §"Observability: OpenTelemetry as the Foundation" + §"Open Standards for Enterprise Agents"*

---

### 1.7 Bounded-step planner + explicit bankruptcy declaration

**Mechanism (Microsoft Armchair Architects):** the agent loop should have a "simple planner with bounded steps" and an explicit point at which it declares bankruptcy: log as failure, notify a human, trigger agent recovery. The architect's words: "I need to know as an architect when to declare bankruptcy in my agent execution — it has just gone off and I'm not going to try to reset it 30 times."

Implementation shape:
```python
for step in range(MAX_STEPS):
    if stuck_detector.is_stuck(history):
        log_failure(reason="stuck", history=history)
        notify_human(...)
        return AgentResult.BANKRUPTCY
    ...
```

**Why durable:** bounded-step + bankruptcy is the standard "don't spin forever" loop termination. The specific `MAX_STEPS` constant varies by use case but the shape is universal.

**Lancelot byte-path:** this is the agent LOOP itself — the part Lancelot lacks. `lancelot.context.agent_id` + the RED metrics can log the bankruptcy event, but the loop and the `MAX_STEPS` budget are pure build targets.

*Source: TRANSCRIPTS_MICROSOFT.md §"The Architect's Agent Implementation Checklist, Phase 4: Reasoning Scaffolding"*

---

### 1.8 State lifecycle prefixes — scoped key-value convention

**Mechanism (ADK TypeScript):** ADK state keys have lifecycle-encoded prefixes:
- `user:key` — persists across sessions for that user
- `temp:key` — lasts only for the current turn
- *(no prefix)* — lasts for the current session

This is enforced by the ADK `Session` / `StateService` layer, not by the LLM. Instructions can reference state via `{key}` template syntax.

**Why durable:** the three-scope model (turn / session / user) maps precisely to the agent memory layers that every production system needs. The prefix convention is a clean encoding that doesn't require a separate config.

**Lancelot byte-path:** `lancelot.context` ContextVars are request-scoped only — they reset per HTTP request. There's no session-scoped or user-scoped persistent state in Lancelot today. This is a core build target for the agent memory layer.

*Source: TRANSCRIPTS_GOOGLE.md §"Session, State, and Memory (Three Components)"*

---

## 2. Production patterns worth copying mechanically

### 2.1 `agent.json` as the A2A discovery manifest

**Mechanism:** when `to_a2a(root_agent, port=8001)` is called and the process is started with uvicorn, the `agent.json` file is auto-created at the root URL. It contains: name, description, tool list (with descriptions), auth mechanisms, URL. The A2A client fetches this to discover what the remote agent can do — no prior knowledge needed.

```json
{
  "name": "finance_agent",
  "description": "Financial approval agent",
  "tools": [{"name": "get_approval_threshold", "description": "..."}],
  "url": "http://localhost:8001",
  "authentication": {}
}
```

**Lancelot byte-path:** Lancelot has `@app.tool` + `lancelot.mcp.mount`. Adding an auto-generated `agent.json` at `/agent.json` (populated from `@app.tool` schemas + `lancelot.mcp` tool list) would make any Lancelot app A2A-discoverable with zero extra code. This is a small addition to the ASGI seam — a const-tier prebaked GET route that serializes tool schemas.

*Source: TRANSCRIPTS_GOOGLE.md §"agent.json: The A2A Discovery Mechanism"*

---

### 2.2 MCP async task IDs — standardized long-running jobs

**Mechanism (MCP v20251125):** the November 2025 MCP version added async task support:

```json
// Start task
{"method": "tasks/create", "params": {"toolName": "long_analysis"}}
// Response: {"taskId": "task-123", "status": "running"}

// Poll
{"method": "tasks/get", "params": {"taskId": "task-123"}}
// Response: {"status": "completed", "result": {...}}
```

**Why relevant:** `lancelot.mcp.mount` currently returns tool results synchronously. For tools that call LLMs (which take >5s), the pattern above prevents connection timeout and allows the client to poll or use SSE. Lancelot's SSE support (`@app.stream`) + the bounded R3 backpressure pump is the right transport layer for the "push result when done" variant.

**Lancelot byte-path:** `lancelot.mcp.mount` should support async task IDs. The task store can be in-memory (a ContextVar-scoped dict). The `tasks/get` poll endpoint is a const-tier route if the task is already done. Lancelot's graceful drain (`LANCELOT_DRAIN_SECS`) already handles the "task running when SIGTERM arrives" case.

*Source: TRANSCRIPTS_MICROSOFT.md §"November 2025 MCP Version (20251125)"*

---

### 2.3 Tool description as the routing signal — precision requirement

**Mechanism (ADK TypeScript + Microsoft Foundry):** the LLM uses the `description` field — not the function name — to decide when to call a tool. Low `ToolAccuracyEvaluator` scores indicate "too many similar tools" or poorly written descriptions. Recommended pattern:

```typescript
const orderStatusTool = new FunctionTool({
    description: `Get the current status of a customer's order.
    Call this when users ask about their order or delivery.`,
    parameters: z.object({
        orderId: z.string().describe("The order ID number (format: ORD-XXXXX)")
    }),
    ...
});
```

The `describe("The order ID number (format: ORD-XXXXX)")` on the parameter is load-bearing — it tells the model the expected format, reducing extraction errors.

**Lancelot byte-path:** `lancelot.tool` auto-derives JSON schema from type hints. Parameter-level `.describe()` is not surfaced today. Adding a `description=` kwarg to `@app.tool`'s parameter extraction (or supporting `Annotated[str, ToolParam(description="...")]`) would encode this pattern directly.

*Source: TRANSCRIPTS_GOOGLE.md §"Function Tools: Complete Pattern" + TRANSCRIPTS_MICROSOFT.md §"Evaluation Framework"*

---

### 2.4 Workflow agents vs LLM agents — the determinism split

**Mechanism (ADK TypeScript):** three agent types with explicit routing rule: "if you can draw a flowchart for it, use a workflow agent." `SequentialAgent`, `ParallelAgent`, `LoopAgent` all bypass the LLM for control flow — the LLM runs inside each sub-step, not as the router.

```typescript
const researchPipeline = new SequentialAgent({
    sub_agents: [webResearchAgent, analysisAgent, reportingAgent]
    // Control flow is deterministic; only sub-agent CONTENT is LLM
});

const storyWriter = new LoopAgent({
    sub_agents: [draftingAgent, critiqueAgent],
    max_iterations: 10,
    // Exit when critiqueAgent is satisfied OR max_iterations
});
```

`LoopAgent` with `max_iterations` is exactly the bounded-step bankruptcy pattern from §1.7 — same primitive from two independent sources.

**Lancelot byte-path:** `TransportPool.fanout` covers the `ParallelAgent` transport layer (parallel HTTP calls with first_n/deadline). Sequential and loop are pure control flow — no transport needed. The Lancelot agent layer needs `SequentialPipeline`, `ParallelFanout`, `LoopUntil(max_iter)` as composable primitives. `TransportPool.fanout` is the byte-path substrate for `ParallelFanout`.

*Source: TRANSCRIPTS_GOOGLE.md §"Workflow Agent Types"*

---

### 2.5 Ring-release deployment for agents

**Mechanism (Microsoft Foundry blueprint):** canary 5%, expand by rings. Monitor cost AND output predictability at each ring before expanding. The specific addition over standard ring-release: **cost is a first-class ring metric** (not just latency/error-rate).

**Lancelot byte-path:** `lancelot.upstream_metrics` already emits `cost_usd` per call. `lancelot.fleet.fleet_info(app)` + region routing (`@app.get('/x', region='eu-west-1')`) are the serving-side primitives. A ring release for agents means: route 5% of `agent_id` traffic to `region='canary'`, watch `lancelot.upstream.cost_usd` histogram in Grafana, expand. The primitives exist; the orchestration is a deploy-infra concern.

*Source: TRANSCRIPTS_MICROSOFT.md §"Production Deployment, Phase 6: Deploy Safely"*

---

### 2.6 Three-attempt rule — cognitive hygiene as a framework primitive

**Mechanism (Sid Anand Carey, quoted in TRANSCRIPTS_MICROSOFT.md):** "try with AI three times; if it doesn't get it by turn three, do it yourself." Described as a recovery from AI-agent burnout, but it encodes something structurally important: **an agent loop that retries more than N times without human intervention is a smell, not a feature**.

This converges with §1.7 (bankruptcy declaration at `MAX_STEPS`). The constant is different (3 vs. some config), but the principle is the same: bound retries explicitly, fail fast to human escalation.

**Lancelot byte-path:** the agent loop (build target) should expose `max_retries_before_escalation` as a first-class config, not buried in loop code.

*Source: TRANSCRIPTS_MICROSOFT.md §"AI Fatigue / The AI Brain Fry" (Sid Anand Carey pattern)*

---

### 2.7 KV cache routing constraint — sticky-shard by prefix hash

**Mechanism (Google Smoke Jumpers):** Gemini's lower cache hit rate (acknowledged deficit) is a **routing problem**: requests with the same prefix must hit the same machine to reuse the KV cache. Standard load balancers distribute randomly. The fix requires consistent hashing on the prompt prefix hash at the load balancer / serving layer.

This is the infrastructure complement to `lancelot.canonical.dumps_canonical` — which produces a stable byte prefix for caching. Stable prefix bytes are necessary but not sufficient; the routing must be sticky on that prefix.

**Lancelot byte-path:** `lancelot.session_affinity` (`Response.set_session_affinity(key)`) is EXACTLY this primitive. The CLAUDE.md calls this out as "KV-cache-aware routing for the agent backend." The Smoke Jumpers transcript validates it as a real, acknowledged production gap at Google scale. `dumps_canonical` produces the stable key; `set_session_affinity` routes to the right shard. No new code needed — the byte path already solves this.

*Source: TRANSCRIPTS_GOOGLE.md §"KV Cache and Cache Hit Rates"*

---

### 2.8 Approval mode on MCP tool calls — per-tool human gate

**Mechanism (Microsoft Agent Framework):** `HostedMCPTool(url=..., approval_mode="never_require" | "always_require" | "none")`. This is per-tool, not per-agent. Read-only MCP tools (Microsoft Learn docs fetch) run `never_require`; write MCP tools (send email) run `always_require`.

**Lancelot byte-path:** `lancelot.mcp.mount` currently has the `>4KB WARN` discipline (minimal-return). There is no per-tool `approval_mode` concept. Adding `approval_mode: Literal["auto", "ask", "deny"]` to `@app.tool` metadata is a small addition with big operational value — an agent framework can inspect this metadata before executing a tool call, surfacing the `"ask"` tools for human review while running `"auto"` tools inline.

*Source: TRANSCRIPTS_MICROSOFT.md §"Microsoft Agent Framework Concepts, approval_mode options"*

---

## 3. Anti-patterns (explicit do-not-do)

### 3.1 One uber-agent that knows everything

**Microsoft:** "I would much rather have you build a thousand agents with very very limited scope that know only what they need to do... The all-knowing, all-understanding software agent frightens me personally." Skynet reference used twice.

**Why it fails technically:** small-scope agents are more predictable, cheaper to eval, cheaper to run (right-size model per task), and fail safely. A 10k-token context uber-agent that knows billing AND legal AND code is a correctness and cost disaster.

*Source: TRANSCRIPTS_MICROSOFT.md §"Agent Infrastructure Requirements" + §"The Skynet Warning"*

---

### 3.2 Storing API keys in environment variables in production

**Google (Aaron Idleman):** "You'd be surprised how frequently that happens in production." Correct pattern: IAM credentials → Secrets Manager → key. The environment variable as production secret is a supply chain liability.

**Lancelot byte-path:** `lancelot.secrets` (5 backends + SIGHUP) already exists and directly addresses this. The agent layer must use `lancelot.secrets.get("api_key")` not `os.environ["API_KEY"]`.

*Source: TRANSCRIPTS_GOOGLE.md §"API Key Pattern (Simpler Use Cases)"*

---

### 3.3 Agents as microservices with direct peer-to-peer calls

**Microsoft:** "Agents are not microservices — peer-to-peer communication between agents is NOT a preferable pattern. When agents talk directly peer-to-peer via agent protocol, it becomes a lot more difficult to instrument, understand, and orchestrate."

The correct shape is supervisor-worker or pub/sub — not flat RPC mesh. This maps directly to the orchestrator-subagent pattern.

*Source: TRANSCRIPTS_MICROSOFT.md §"A2A vs. MCP: The Clear Distinction"*

---

### 3.4 Using the same model for every task

**Multiple sources:** GPT-5 Codex for the coding agent (specialist model), gemini-2.0-flash for orchestration, GPT-5.4 for image generation in a sub-agent — each task gets a right-sized model. "How smart does it really need to be? Do I need full-blown GPT-5 or can I use a smaller, faster, domain-specific model?"

Cost warning from Microsoft: "In the early days of cloud computing I'd design galactic infrastructure — people would run it for a month and be like 'this is a million dollars a month.'" Same applies to agents using frontier models for every call.

**Lancelot byte-path:** `lancelot.upstream_metrics` + `TransportPool` support any model endpoint. The agent framework's model adapter (build target) should include `model=` per-agent config and the `cost_usd` ContextVar will automatically accumulate multi-model costs.

*Source: TRANSCRIPTS_MICROSOFT.md §"Enterprise Agent Context" + §"AI Agent Development Blueprint"*

---

### 3.5 Blocking architecture for long-running agent tasks

**Microsoft:** "Most of these scenarios will switch to message-based patterns simply because we will have long-running operations. Blocking resources with synchronous calling doesn't make sense for long-running agent tasks."

The `tasks/create` + `tasks/get` MCP async pattern (§2.2) is the concrete fix. For agent-to-agent, A2A with SSE push notifications is the fix.

**Lancelot byte-path:** Lancelot's entire streaming stack (R3 pump, SSE, `@app.stream`) is designed for exactly this. The `TransportPool.fanout` + `first_n`/`deadline` handles the "start N, take first M" pattern for parallel agent calls. These are already Lancelot wins.

*Source: TRANSCRIPTS_MICROSOFT.md §"Message-Based Architecture for Multi-Agent Systems"*

---

## 4. Hype / depreciating tactical patterns (skip or flag)

**MCP Registries (Smithery, MCP Hub, Awesome MCP, Glama.ai):** directoryof-MCP-servers lists. These churn monthly. Don't build integration with any specific registry; build MCP client capabilities and let users point at any URL.

**YOLO mode (GitHub Copilot):** "grant all authorizations without prompting" for trusted workflows. This is a developer DX feature, not an agent architecture primitive. Mention but don't build.

**AutoGen → Azure AI Fabric merge:** AutoGen (Microsoft Research multi-agent framework) is being absorbed into Azure AI Fabric. The pattern library is sound; the specific SDK is churn. Copy the patterns (§1.7, 2.4), not the import path.

**Multi-model router APIs:** "submit a request, an intelligent system picks the right model." The router itself is model-powered — subject to the same capability churn as the models it routes. The *principle* (right-size model per task) is durable (§3.4 above); the managed-router product layer is tactical.

**Ring-release `agentVersion` field on ChatKit:** `agentVersion: "v2"` instant rollback pattern is a managed-platform feature (Azure AI Foundry). It's a clean DX pattern but the specific API surface churns with the platform.

---

## 5. API shape notes worth stealing

### 5.1 Handoff builder fluent API (Microsoft)

```python
workflow = (HandoffBuilder()
    .name("developer_onboarding")
    .participants([triage_agent, employee_agent, learning_agent, coding_agent])
    .set_coordinator(triage_agent)
    .add_handoff([employee_agent, learning_agent, coding_agent])
    .add_handoff([learning_agent], target=coding_agent)  # direct handoff bypass
    .termination(max_turns=20)
    .build()
)
```

`max_turns=20` on `.termination()` is the bankruptcy declaration baked into the builder — clean ergonomics for §1.7.

### 5.2 ADK state prefix convention

`"user:key"` / `"temp:key"` / `"key"` — the prefix encodes scope. Simple to implement in any dict-backed state store. Alternative: `Scope.USER / TURN / SESSION` enum — either works.

### 5.3 Agent-as-tool wrapping (ADK)

```typescript
const summarizerTool = new AgentTool(summarizerAgent);
// Wraps an agent so another agent can call it as if it were a function tool
```

This is the clean way to compose agents without the caller knowing whether it's talking to a function or a sub-agent. The sub-agent's `description` field is what the outer LLM uses for routing — same signal as tool description (§2.3).

### 5.4 `to_a2a()` one-liner

```python
A2A_app = to_a2a(root_agent, port=8001)
# uvicorn A2A_app:app --host 0.0.0.0 --port 8001
# → auto-creates /agent.json, serves A2A protocol
```

Lancelot equivalent: `lancelot.a2a.wrap(app, port=8001)` — a thin adapter over the existing ASGI app that adds `/agent.json` + A2A JSON-RPC endpoints.

---

## 6. Convergent signals (cross-validated with other sources)

| Pattern | Microsoft | Google | Validates |
|---|---|---|---|
| Bounded steps + bankruptcy | ✓ (MAX_STEPS) | ✓ (LoopAgent max_iterations) | Two independent sources → durable |
| Tool description = routing signal | ✓ (ToolAccuracyEvaluator) | ✓ (ADK tutorial) | Parameter-level descriptions required |
| Small specialists > uber-agent | ✓ (Skynet warning) | ✓ (purpose-built agents only) | Strong convergence |
| Auth inside tool, not agent | ✓ (MCP pass-through question) | ✓ (ADK before_model pattern) | Durable security shape |
| Least-privilege permissions | ✓ (OAuth token = task TTL) | ✓ (excessive agency = OWASP #4) | Universal |
| SSE for long-running agent ops | ✓ (async MCP tasks) | ✓ (A2A SSE push notifications) | Matches Lancelot's R3 pump |
| Right-size model per task | ✓ (GPT-5 Codex specialist) | ✓ (gemini-2.0-flash vs frontier) | Cost + quality |
| KV cache routing = prefix stickiness | implicit | ✓ (Smoke Jumpers) | `session_affinity` already built |

---

## 7. What Lancelot already has that these sources validate (no build needed)

1. **`session_affinity` / `dumps_canonical`** — KV cache routing (Smoke Jumpers §2.7). Already built and correctly positioned.
2. **`TransportPool.fanout`** — parallel agent call substrate (ADK ParallelAgent). Already built.
3. **`lancelot.secrets`** — secrets-manager pattern (Google API key §3.2). Already built.
4. **R3 SSE streaming pump** — SSE push for long-running ops (A2A, async MCP tasks §2.2). Already built.
5. **`lancelot.upstream_metrics` cost tracking** — cost-as-ring-metric (§2.5). Already built.
6. **`traceparent` propagation** — OTel agent span correlation (§1.6). Already built.
7. **`lancelot.mcp.mount` minimal-return >4KB WARN** — prevents over-large tool responses. Already built.

---

*Note path: `/Users/seventyleven/Desktop/Lancelot/research/02_core/transcripts_msft_google.md`*
