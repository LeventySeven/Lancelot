# Deep Research teardown — mechanism harvest

Sources: `CLAUDE_RESEARCH.md`, `OPENAI_DEEP_RESEARCH.md`, `GEMINI_DEEP_RESEARCH.md`, `DEEP_RESEARCH_FAST_MODE_RE.md` (all in `researchfms/teardowns/`). Focus areas: define_outcome grader, orchestrator-workers topology, URL gate, source-quality removal mid-flight, fast/full route classification.

---

## 1. Orchestrator-workers topology: depth limit = 1

**The enforced constraint (Anthropic).** `multiagent_params.py:23`: worker agents may NOT have `multiagent` set. Depth limit = exactly 1. Workers research; the orchestrator (lead) synthesizes and writes the final report. The lead is explicitly prohibited from delegating final-report writing: `research_lead_agent.md:151` — "NEVER create a subagent to generate the final report — YOU write and craft this final research report yourself." (CLAUDE_RESEARCH.md §R3.13, §CE3.4)

**Spawn contract — required delegation fields:**
1. `objective` — exactly one core objective per worker
2. `output_format` — what to return
3. `background_context` — why this matters in the larger run
4. `key_questions` — what to answer
5. `suggested_sources` + `unreliable_sources_to_avoid` — source guidance
6. `specific_tools` — which tools to grant
7. `scope_boundaries` — what is out of scope

(CLAUDE_RESEARCH.md §CE3.4)

**Roster cap.** Max 20 distinct agents. `{"type":"self"}` is the legal form for bounded recursion (at most one per roster). By complexity tier (CLAUDE_RESEARCH.md §CE3.3, verbatim from `research_lead_agent.md:71-86`):

```
Simple:   1 subagent
Standard: 2-3
Medium:   3-5
High:     5-10 (max 20)
Default:  3 subagents
```

**Spawn tool name.** In the `managed_agents` SDK (v0.104.1), the client-emit tool name is `run_blocking_subagent` (not `delegate_subagent`). Completion signal is `complete_task`. These are the only two correct names for the spawn / done contract. (CLAUDE_RESEARCH.md §R3)

**Parallel fan-out.** Lead prompt (CLAUDE_RESEARCH.md §CE3.6, verbatim from the leaked prompt): "You MUST use parallel tool calls for creating multiple subagents (typically running 3 subagents at the same time)." Worker prompt: "whenever you need to perform multiple independent operations, invoke 2 relevant tools simultaneously." Parallel means multiple `run_blocking_subagent` tool_use blocks in one assistant turn; the harness handles `asyncio.gather`.

---

## 2. define_outcome grader — bounded generate→grade→revise

**Default budget.** 3 revisions. Max 20 revisions. This is the in-run quality loop: generate → judge (via `define_outcome` rubric) → revise; repeat up to the revision cap. (CLAUDE_RESEARCH.md §R3.13)

**Rubric ceiling.** `TextRubric` max 262,144 chars (256 KiB). The rubric is passed as text to a judge model. (CLAUDE_RESEARCH.md §R3.13)

**`advisor` tool.** Cheap inline single-model judge for mid-run quality checks short of a full `define_outcome` cycle. Use for lightweight "is this good enough to proceed?" checks between research steps. (CLAUDE_RESEARCH.md §R3)

**Five verdict states.** The grader returns one of: pass / needs_revision / needs_more_research / needs_restart / unresolvable. On `unresolvable`, log and stop — do not loop infinitely. (CLAUDE_RESEARCH.md §R3)

**CitationAgent post-pass.** A separate model call runs AFTER the lead writes the citation-FREE final report. The validation gate: "Text without citations will be collected and compared to the original report from the `<synthesized_text>`. If the text is not identical, your result will be rejected." — byte-identical match enforced. (CLAUDE_RESEARCH.md §CE3.7)

---

## 3. URL gate — structural SSRF defense

**Anthropic.** `web_fetch` only accepts URLs from: (a) the user's original message, or (b) prior `web_search`/`web_fetch` results from the same session. URLs hallucinated mid-run are rejected. This is enforced structurally — the tool schema doesn't accept arbitrary URLs. (CLAUDE_RESEARCH.md §R3.13)

**OpenAI parallel.** The worker system prompt (Feb 4 2025 host): "Worker cannot cite URLs not actually fetched via tool." Same constraint: you can only cite what you actually opened through the `web` namespace tools. (OPENAI_DEEP_RESEARCH.md §R1)

**Why this matters.** Both are structural SSRF-adjacent defenses at the agent harness level. An agent that trusts user-supplied URLs without gating has a prompt-injection path: "fetch http://internal-host/secret". The gate closes this without requiring the model to reason about it. Lancelot's `lancelot.net` SSRF guard is the transport-layer analogue — the agent-loop version of this is a URL allowlist at the `web_fetch` tool schema level.

---

## 4. Source-quality: tool removed mid-flight lesson

**The failure.** During an OpenAI deep-research run, the `file_search` tool was removed from the available toolset mid-run (the store had been deleted or deprovisioned). The model continued calling it and received failures. The harness did NOT gracefully degrade: the model's tool_use blocks referenced a non-existent tool, producing cascading errors. (OPENAI_DEEP_RESEARCH.md §R2)

**The lesson.** Tool availability must be validated at the START of a run, not assumed throughout. If a tool disappears mid-flight (MCP server down, file store deleted, rate-limit-disabled), the agent needs an explicit fallback path — not infinite retries or silent failure. The harness should verify the tool manifest before the first research step, not inline during calls.

**Gemini parallel.** Gemini's `file_search_store` (`{ "type": "file_search", "file_search_store_names": [...] }`) is declared in the API request body. If the store doesn't exist, the call fails at routing time — earlier feedback than OpenAI's mid-run discovery. (GEMINI_DEEP_RESEARCH.md §R2)

**For Lancelot.** `lancelot.mcp.mount` warns on tool return >4 KB (minimal-return discipline). The parallel lesson: validate tool availability (MCP server reachable, file stores present, rate limits not already exhausted) at agent startup, emit a structured `ToolUnavailable` error rather than letting the agent discover failures mid-loop.

---

## 5. Fast/full route classification

**Three-way query classifier (Anthropic).** (CLAUDE_RESEARCH.md §CE3.8, inferred from `research_lead_agent.md`):
- `depth-first`: multi-perspective exploration of one topic → spawn serial deep-dive workers
- `breadth-first`: distinct independent sub-questions → spawn parallel workers per sub-question
- `straightforward`: single lookup / factoid → one worker or inline answer

No complex routing logic: classify up front, commit to a spawn topology, do not switch mid-run.

**FAST-mode constants (open-source clones, evidence-anchored).** From 5 clones: `open_deep_research`, `smolagents`, `local-deep-research`, `gpt-researcher`, `dzhng`. (DEEP_RESEARCH_FAST_MODE_RE.md §S2):

```python
FAST_MAX_STEPS = 6              # open_deep_research supervisor cap = 6
FAST_MAX_QUERIES_PER_STEP = 4   # dzhng breadth=4
FAST_MAX_RESULTS_PER_QUERY = 5  # two clones agree on 5
FAST_MIN_NEW_DOMAINS = 2        # "last 2 searches returned similar info"
FAST_STALL_PATIENCE = 1         # stop after FIRST stalled step in fast mode
FAST_MIN_SOURCES_PER_SUBQ = 3   # "3+ relevant examples/sources"
FAST_MAX_SUBQUESTIONS = 3       # gpt-researcher MAX_SUBTOPICS=3
RESERVE_FOR_SYNTHESIS = 0.25    # 25% of budget reserved for write step
FAST_CONTENT_TRIM_CHARS = 25000 # dzhng trimPrompt(content, 25_000)
FAST_TEMPERATURE = 0.4          # gpt-researcher TEMPERATURE=0.4
FAST_CONFIDENCE_STOP = 0.85     # local-deep-research iterative_reasoning threshold
FAST_PLANNING_INTERVAL = 3      # smolagents planning_interval=4, we use 3
```

**Four-clause STOP rule.** (DEEP_RESEARCH_FAST_MODE_RE.md §S3):
1. Hard cap: `t >= FAST_MAX_STEPS`
2. Coverage checklist all-green: all sub-questions at >= `FAST_MIN_SOURCES_PER_SUBQ` (3) sources
3. Diminishing returns: `< FAST_MIN_NEW_DOMAINS` (2) new distinct domains for `FAST_STALL_PATIENCE` (1) consecutive steps
4. Model self-declares: emits `research_complete: true` in structured output

**`open_deep_research` full-mode constants.** (DEEP_RESEARCH_FAST_MODE_RE.md §S2):
```
max_structured_output_retries: 3
max_concurrent_research_units: 5
max_researcher_iterations: 6   (supervisor cap)
max_react_tool_calls: 10       (per-researcher cap, ReAct inner loop)
max_content_length: 50000      (per fetched page, chars)
research_model_max_tokens: 10000
final_report_model_max_tokens: 10000
```

**SimpleQA-optimal config** (benchmarked, `local-deep-research`): 8 iterations × 5 questions/iter = 40 queries → 90%+ accuracy. (DEEP_RESEARCH_FAST_MODE_RE.md §S2)

**Budget allocation.** Reserve 25% of step budget for synthesis (the write step). If `FAST_MAX_STEPS=6`, last research step fires at t=4 (step 5 reserved for synthesis). (DEEP_RESEARCH_FAST_MODE_RE.md §S2)

---

## 6. Worker budget constants (Anthropic)

Verbatim from `research_subagent.md` (CLAUDE_RESEARCH.md §CE3.5):

```
Under 5 tool calls:  simple tasks
5 tool calls:        medium tasks
About 10:            hard tasks
Up to 15:            very difficult / multi-part
Minimum floor:       5 distinct tool calls
Soft stop:           ~15 calls OR ~100 sources → call complete_task
Hard kill:           20 calls / 100 sources → terminated by harness
```

**Diminishing-returns stop.** `research_lead_agent.md:150`: when further research has diminishing returns, STOP — call `complete_task`. This is the only mention; no numeric threshold. The harness enforces the hard kill; the soft stop is a model-judgment call.

---

## 7. Lazy tool loading (`tool_search`)

Only `web_search` / `web_fetch` / `tool_search` are present in the initial context. All domain tools are deferred. `tool_search(query, limit≤20)` retrieves tool schemas on demand. Default limit=5, max=20. This keeps the initial system-prompt token footprint small and lets the model request tools it actually needs. (CLAUDE_RESEARCH.md §R3.13)

**For Lancelot.** `lancelot.mcp.mount` currently dumps all tool schemas at mount time. The deferred-load pattern is worth copying for large tool surfaces: expose a `tool_search` primitive in `lancelot.mcp`, return schemas only for tools the agent asks for. Keeps per-request context small.

---

## 8. Memory: POSIX directory, not KV

**Anthropic.** Memory is a POSIX directory at `/mnt/memory/<name-slug>/`. Agents use file creation / read / list / delete tools. There is no `memory_write(key, value)` API. (CLAUDE_RESEARCH.md §R3.13)

**Why POSIX.** Supports structured sub-directories, append-only logs, binary blobs, and large files without a KV size limit. File tools are already in the agent's context; no extra memory API needed.

---

## 9. OpenAI: three-channel architecture + memento

**Three output channels.** (OPENAI_DEEP_RESEARCH.md §R1):
- `analysis`: hidden reasoning (not user-visible)
- `commentary`: user-visible progress updates + tool calls
- `final`: deliver results or confirm before irreversible steps

**`memento` self-summarization.** (OPENAI_DEEP_RESEARCH.md §R2, Aug 9 2025 GPT-5 Agent Mode):
```typescript
type memento = (_: {
  analysis_before_summary?: string,
  summary: string,
}) => any;
```
"If you need to think for longer than 'Context window size' tokens you can use memento to summarize your progress." Analysis-channel only; never user-visible. Prevents context-overflow mid-run by letting the agent checkpoint its own state into a compact summary.

**Citation format.** `【{cursor}†L{line_start}-L{line_end}】` — line-level references, cursor is the source ref_id from the `web` namespace fetch. (OPENAI_DEEP_RESEARCH.md §R1)

---

## 10. Gemini: collaborative_planning + SSE

**`collaborative_planning` param** (GEMINI_DEEP_RESEARCH.md §R2):
1. POST with `collaborative_planning: true` → agent emits plan and stops (no research)
2. User inspects, edits via `previous_interaction_id`
3. Final POST with `collaborative_planning: false` → executes

This is an explicit plan-gate before execution. Lancelot's analogue would be a two-phase `agent.plan()` / `agent.run()` split surfaced as an API.

**SSE streaming.** Events: `interaction.created`, `step.delta` (`delta_type: thought|text|image`), `interaction.completed`, `error`. Resumable via `last_event_id`. `background=true` + `store=true` required for async runs. (GEMINI_DEEP_RESEARCH.md §R2)

**Search grounding shape.** `groundingSupports[].{segment:{startIndex,endIndex,text}, groundingChunkIndices[], confidenceScores[]}` — per-segment confidence scores, not binary. Useful for selective citation extraction.

---

## Anti-patterns (do NOT copy)

1. **`delegate_subagent` as spawn tool name** — WRONG. The correct Anthropic tool name is `run_blocking_subagent`. (CLAUDE_RESEARCH.md §R3)
2. **"80% overlap eviction threshold" for context compaction** — FABRICATED. Context compaction in the managed_agents SDK is summarization-based, not a rule-based eviction threshold. No such constant exists. (CLAUDE_RESEARCH.md §CE.10 — claim not supported)
3. **`memory_write(key, value)` KV API** — WRONG. Memory is a POSIX directory. File tools only. (CLAUDE_RESEARCH.md §R3.13)
4. **Pushing all tool schemas at spawn** — WRONG. Only web_search/web_fetch/tool_search appear in initial context; remaining tools are lazily fetched via `tool_search`. (CLAUDE_RESEARCH.md §R3.13)
5. **PPX-4 T5-XXL / entropy=0.85 claims** — DEBUNKED SPECULATION. Traced to `OmidZamani/perplexity-journey`, refuted by primary Perplexity article. Do not reference as Perplexity internals. (DEEP_RESEARCH_FAST_MODE_RE.md §S4)
6. **Recursive multi-level orchestration (depth > 1)** — PROHIBITED by the managed_agents SDK. Workers cannot have `multiagent` set. Implement `{"type":"self"}` only for controlled single-level recursion. (CLAUDE_RESEARCH.md §R3.13)
7. **Delegating final-report writing to a subagent** — EXPLICITLY PROHIBITED. `research_lead_agent.md:151`. The lead writes the report; workers only research and return findings. (CLAUDE_RESEARCH.md §CE3.4)

---

## Byte-path implication (what Lancelot already covers)

| Primitive | Lancelot byte-path coverage |
|---|---|
| Parallel fan-out (N workers simultaneously) | `TransportPool.fanout(coros, first_n, deadline, cancel_laggards, concurrency)` — already built |
| Worker context isolation (separate request_id / trace) | `TransportPool.spawn_child(parent, session_affinity)` + `lancelot.context` ContextVars |
| Cost / token accumulation across sub-calls | `lancelot.context.cost_usd + tokens_in/out` + `X-Lancelot-Cost-USD` trailer |
| Tool dispatch + MCP mount | `lancelot.mcp.mount` + `@app.tool` |
| Streaming TTFT + backpressure | `lancelot.stream` R3 pump + `lancelot.http.stream.ttft` metric |
| Prompt-cache stability | `lancelot.canonical.dumps_canonical` |
| Request coalescing to rate-limited backends | `lancelot.batch` |
| SSRF guard at transport | `lancelot.net` (block_private_ips on TransportPool) |

**What is NOT covered (the build targets for the agent layer):**
- The agent loop itself (ReAct, budget enforcement, stuck detect, grace call)
- `define_outcome` grader (generate→grade→revise, 5 verdict states, rubric ceiling)
- Three-way query classifier (depth-first / breadth-first / straightforward)
- Per-subagent budget enforcement (soft stop at 15 calls / hard kill at 20)
- CitationAgent post-pass (separate model call, byte-identical validation)
- Lazy tool loading / `tool_search` primitive in `lancelot.mcp`
- Memory filesystem mount (POSIX dir at a well-known path)
- FAST-mode stop heuristics (4-clause rule + constants table)
- URL allowlist gate at `web_fetch` schema level
- `collaborative_planning` two-phase plan-gate API
- `memento` self-summarization for context overflow

---

*Sources*: `researchfms/teardowns/CLAUDE_RESEARCH.md`, `OPENAI_DEEP_RESEARCH.md`, `GEMINI_DEEP_RESEARCH.md`, `DEEP_RESEARCH_FAST_MODE_RE.md`. All constants cited inline to section/file. No secondary sources used.
