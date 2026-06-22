# Claude Agent SDK — Mechanism-Level Distillation

**Source:** `researchfms/teardowns/CLAUDE_AGENT_SDK.md` (3 rounds, 2465 lines, covering `claude-agent-sdk==0.1.81` Python + `@anthropic-ai/claude-agent-sdk@0.3.144` TypeScript)
**Date of teardown:** 2026-05-15 (R2), 2026-05-20 (Coverage Expansion), 2026-05-30 (R3)

---

## Positioning against Lancelot

The Claude Agent SDK is a **subprocess bridge** — it does not implement an agent loop in user code. All agent behavior (tool calling, planning, sub-agents, compaction, permission gating) runs inside a bundled Bun-compiled `claude` CLI binary. The SDK is ~10,561 Python LOC of JSONL control-protocol glue.

**Anti-pattern for Lancelot:** do NOT replicate the subprocess-delegated loop. This is a single-vendor lock-in architecture (Anthropic CLI only). The durable primitives worth copying are: the **10/29 hook event system**, the **6-mode permission model**, the **in-process MCP routing pattern**, and the **pluggable SessionStore transcript-mirror design**.

---

## Mechanism 1: The Control Protocol (Bidirectional JSONL RPC)

**What it is:** synchronous request/response over an async JSONL stream. Any control plane call from either side uses the same channel as user messages.

**Wire format** (`query.py:501-546`, KNOWN):
```jsonc
// SDK → CLI
{ "type": "control_request", "request_id": "req_42_a1b2c3d4",
  "request": { "subtype": "initialize", ... } }

// CLI → SDK (success)
{ "type": "control_response", "response": { "subtype": "success",
  "request_id": "req_42_a1b2c3d4", "response": {...} } }

// CLI → SDK (error)
{ "type": "control_response", "response": { "subtype": "error",
  "request_id": "req_42_a1b2c3d4", "error": "Tool permission denied" } }

// CLI → SDK (cancel in-flight)
{ "type": "control_cancel_request", "request_id": "req_42_a1b2c3d4" }
```

**Request ID format:** `f"req_{counter}_{os.urandom(4).hex()}"` (`query.py:515`). Per-Query counter + 4-byte random suffix. The random suffix prevents collisions across reconnects where the counter resets.

**Correlation mechanism** (`query.py:114-115`):
- `pending_control_responses: dict[str, anyio.Event]` — the event the requester blocks on
- `pending_control_results: dict[str, dict | Exception]` — the resolved value or exception
- On `control_response` arrival: look up event, store result, call `event.set()` to wake caller
- Timeout: **60 seconds** default (`query.py:502`). `initialize` also 60s default (`query.py:83`)

**13 control subtypes** (full enumeration, `query.py:723-807`):

Outbound SDK→CLI:
1. `initialize` — handshake; ships hooks/agents/skills/excludeDynamicSections; must be first
2. `interrupt` — abort current turn
3. `set_permission_mode` — change permission mode mid-session
4. `set_model` — change model mid-session
5. `mcp_status` — get current MCP server connection status
6. `get_context_usage` — per-category token breakdown (system_prompt/tools/messages/mcp_tools/memory_files)
7. `rewind_files` — rewind file checkpoints to a prior message UUID (requires `enable_file_checkpointing`)
8. `mcp_reconnect` — force-reconnect a disconnected MCP server
9. `mcp_toggle` — enable/disable a named MCP server at runtime
10. `stop_task` — stop a named sub-agent task

Inbound CLI→SDK:
11. `can_use_tool` — permission gate; SDK invokes user callback and returns allow/deny
12. `hook_callback` — invoke a registered Python hook function
13. `mcp_message` — route a JSONRPC call to an in-process SDK MCP server

**Cancellation** (`query.py:279-285`): on `control_cancel_request`, the SDK cancels the in-flight handler. The handler re-raises `CancelledError` WITHOUT writing a response — the CLI dropped the request_id, writing a response would produce a stray correlation log error.

**Lancelot relevance:** Lancelot does not need this protocol (Lancelot owns the agent loop directly, not a subprocess CLI). But the **correlation pattern** (Event + result dict, one Event per request ID, 60s timeout, structured error swap) is a clean RPC primitive worth having for any bidirectional async channel Lancelot opens to external MCP servers or sub-agents.

---

## Mechanism 2: The 29-Event Hook System

**The canonical hook event list** (`sdk.d.ts:738`, 10 in Python SDK / 29 in TypeScript SDK, KNOWN):

```
PreToolUse          PostToolUse         PostToolUseFailure
PostToolBatch       Notification        UserPromptSubmit
UserPromptExpansion SessionStart        SessionEnd
Stop                StopFailure         SubagentStart
SubagentStop        PreCompact          PostCompact
PermissionRequest   PermissionDenied    Setup
TeammateIdle        TaskCreated         TaskCompleted
Elicitation         ElicitationResult   ConfigChange
WorktreeCreate      WorktreeRemove      InstructionsLoaded
CwdChanged          FileChanged
```

**Registration mechanism** (`query.py:181-200`, KNOWN): at `initialize`, each callback function is assigned a unique `hook_{N}` ID and stored in `hook_callbacks`. The wire sends only IDs. When the CLI fires the event, it emits `hook_callback` control_request with the ID; the SDK looks up the function and calls it. The Python functions never cross the wire.

**Hook output shape** (`types.py:493-559`, KNOWN):

```python
SyncHookJSONOutput = {
    "continue_": bool,         # → "continue" wire; False = stop the loop
    "suppressOutput": bool,
    "stopReason": str,
    "decision": "block",
    "systemMessage": str,      # warning shown to user
    "reason": str,             # feedback to the model
    "hookSpecificOutput": {...}
}

AsyncHookJSONOutput = {
    "async_": True,            # → "async" wire; CLI does NOT wait for result
    "asyncTimeout": int,       # ms
}
```

**Key per-hook output fields** (`types.py:411-490`, KNOWN):

`PreToolUse`:
- `permissionDecision: "allow" | "deny" | "ask" | "defer"` — 4 values; `defer` passes to next hook
- `updatedInput: dict` — **rewrite the tool's input arguments before execution**
- `additionalContext: str` — inject text into the model context

`PostToolUse`:
- `updatedToolOutput: Any` — **rewrite the tool's result BEFORE the model sees it**
- For built-in tools (Bash/Read/Edit), this must match the tool's exact output schema (e.g. `{stdout, stderr, interrupted}` for Bash); mismatched shape is rejected silently and original output kept

`UserPromptSubmit`, `SubagentStart`, `Notification`:
- `additionalContext: str` — inject text into model at that point

**Hook permission decision precedence** (`sdk.d.ts:763`, KNOWN): `deny > ask > allow > defer`. First non-defer across the hook chain wins.

**Field name rewriting** (`query.py:42-58`, KNOWN): `async_`→`async`, `continue_`→`continue`. Only two keys. This is because `async` and `continue` are Python keywords.

**Sub-agent attribution** (`types.py:288-304`, KNOWN):
```python
agent_id: NotRequired[str]    # sub-agent identifier when hook fires inside Task
agent_type: NotRequired[str]  # e.g. "general-purpose", "code-reviewer"
```
When 3 parallel sub-agents all call `Bash`, you get 3 interleaved `PreToolUse` events on the same channel; `agent_id` is the only reliable disambiguator.

**Async hook pattern** (`types.py:500-512`): return `{"async_": True, "asyncTimeout": 5000}` and the CLI proceeds immediately without waiting. Use for slow side-effects (remote logging, alerts) that must not block the conversation.

**`opt-in hook events in stream**  (`subprocess_cli.py:407`, KNOWN): `--include-hook-events` makes the CLI emit `HookEventMessage` objects in the user message stream. This is observability-without-callbacks — watch every hook fire as a message without registering callbacks.

**Lancelot relevance:** These are the **agent-level safety primitives** Lancelot lacks. For Lancelot's agent layer:
- Copy `PreToolUse` with `updatedInput` (tool arg rewriting before execution = input sanitization / schema enforcement)
- Copy `PostToolUse` with `updatedToolOutput` (result rewriting = truncation, redaction, format normalisation before the model sees it)
- Copy `async_` hook output shape (non-blocking side-effects pattern)
- The 29-event taxonomy is the canonical list; at minimum implement the 10 Python SDK events for an MVP

---

## Mechanism 3: The Permission System

**6 permission modes** (`types.py:24-26`, KNOWN):
```
default           — prompt the SDK callback per tool call
acceptEdits       — auto-approve file edits (Edit/Write/MultiEdit)
plan              — model can reason but cannot execute tools
bypassPermissions — allow everything (dangerous; requires explicit opt-in flag)
dontAsk           — deny anything not pre-approved by allowed_tools / rules
auto              — a classifier model on the CLI side decides per call
```

The `auto` mode is notable: per `query.py:60-61`, a model classifier approves or denies each call. This is a non-trivial sub-model invocation, not a rule table.

**`can_use_tool` callback** (`query.py:384-436`, KNOWN):
```python
CanUseTool = Callable[[str, dict, ToolPermissionContext], Awaitable[PermissionResult]]
```

`ToolPermissionContext` carries:
- `tool_use_id` — the specific call being evaluated
- `agent_id` — sub-agent attribution (which Task spawned this call)
- `blocked_path` — set when Bash tries a path outside the allowlist
- `decision_reason` — why the CLI is asking
- `suggestions: list[PermissionUpdate]` — suggested rules the user can apply (one-click allowlist expansion)

`PermissionResultAllow(updated_input=...)` — allow and optionally rewrite the tool args  
`PermissionResultDeny(message=..., interrupt=True)` — deny; `interrupt=True` aborts the whole turn

**Wire shape on allow** (`query.py:405-421`, KNOWN): the SDK echoes the original input back as `updatedInput` even when the callback doesn't override it — the CLI always expects an input field.

**5 permission update destinations** (`sdk.d.ts:1935`, KNOWN):
```
userSettings     → ~/.claude/settings.json
projectSettings  → .claude/settings.json (repo-committed)
localSettings    → .claude/settings.local.json (gitignored)
session          → in-memory, this session only
cliArg           → ephemeral, this command only
```

**Restrictive-only `managedSettings` filter** (`sdk.d.ts:1664-1687`, KNOWN): permissive arrays (allow-lists like `permissions.allow`, `additionalDirectories`) are **silently dropped** from embedding-app-supplied policy. Only deny-lists / locks pass through. Defense-in-depth: an embedding app can strengthen an admin lockdown but cannot weaken it.

**`defer` decision** (`sdk.d.ts:763`, KNOWN): the 4th hook permission decision. Passes to the next hook without consuming the decision. Enables hook composition where each hook handles only specific tools.

**Lancelot relevance:** The 6-mode permission model is directly copyable. `Lancelot.agent` needs: an `allow`/`deny`/`ask`/`defer` decision type, a `can_use_tool` callback interface, `blocked_path` attribution, and `updated_input` arg-rewriting on allow. The `session`-tier destination maps naturally to a Lancelot ContextVar.

---

## Mechanism 4: In-Process MCP Routing (Zero-IPC Custom Tools)

**The pattern** (`query.py:548-721`, KNOWN): user defines a Python MCP server with `@tool` decorator + `create_sdk_mcp_server()`. When the CLI calls a tool on that server, it emits an `mcp_message` control_request. The SDK routes it straight to the Python `McpServer.request_handlers` — no subprocess, no socket, no IPC. The round-trip is: CLI→control_request on stdout → Python dispatch → inline response on stdin.

**The JSONRPC router** handles exactly 4 methods:
1. `initialize` — hardcoded `{"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {...}}`. Note: `capabilities.tools = {}` means **no `listChanged`** — the tool list is static for this server's lifetime.
2. `tools/list` — invokes `server.request_handlers[ListToolsRequest]`, maps to `{name, description, inputSchema, annotations, _meta}`.
3. `tools/call` — invokes `CallToolRequest`, converts content blocks:
   - `text` → `{type:text, text: ...}`
   - `image` → `{type:image, data, mimeType}`
   - `resource_link` → flattened text block: name + `\n` + uri + `\n` + description
   - `resource` with text → text block; with binary → **silently dropped** with a warning
   - Unknown types → dropped with warning
4. `notifications/initialized` → `{"jsonrpc":"2.0","result":{}}` no-op.

**Why manually routed** (`query.py:579-584`, comment, KNOWN): Python MCP SDK lacks the `Transport` abstraction TypeScript has. TS does `server.connect(transport)`. Python's `server.run(read_stream, write_stream)` requires real streams. When the Python MCP SDK gains a Transport abstraction, this can be replaced.

**`_build_meta` and `anthropic/maxResultSizeChars`** (`__init__.py:418-428`, KNOWN): the `@tool` decorator sets `_meta.anthropic/maxResultSizeChars` — the CLI's spill threshold. Tools returning larger outputs have their result persisted to disk and a summary reference substituted in context (`toolResultStorage.ts`). This is the canonical mechanism for avoiding context pollution from large tool outputs.

**Tool schema generation** (`__init__.py:234-303`, KNOWN): `_python_type_to_json_schema` handles `Annotated[type, "description"]`, `Optional`, `Union`, `list[T]`, `dict`, TypedDict. Falls back to `"string"` for unknown types (e.g. datetime, UUID, dataclasses). Full type coverage requires manual schema for custom types.

**The `instance` field strip** (`subprocess_cli.py:312-317`, KNOWN): `McpSdkServerConfig` has a runtime-only `instance: McpServer` field stripped before JSON serialization to the CLI. The CLI learns only the server name and type; it routes calls back via `mcp_message`. Clean pattern for keeping non-serializable references local while passing serializable config over the wire.

**Lancelot relevance:** `lancelot.mcp.mount` already provides in-process MCP server mounting. The `_build_meta`/`anthropic/maxResultSizeChars` pattern is worth copying: Lancelot should let `@app.tool` set a `max_result_bytes` threshold (already in spirit — Lancelot warns at >4 KB) and expose it as a tool annotation so MCP clients can apply spill-to-storage.

---

## Mechanism 5: Pluggable SessionStore + Transcript Mirroring

**The design** (`session_store.py`, `transcript_mirror_batcher.py`, KNOWN): instead of burning session transcripts to `~/.claude/projects/`, any backend (Redis, Postgres, S3) can be plugged in by passing a `session_store: SessionStore` object.

**Wire mechanism** (`subprocess_cli.py:346-347`, KNOWN): `--session-mirror` tells the CLI to emit every JSONL entry it would write to disk as a `transcript_mirror` stdout message. The SDK's `_read_messages` loop peels these off (never surfaced to user code), batches them, and calls `store.append()`.

**SessionStore protocol** (`types.py:1369-1486`, KNOWN):
```python
class SessionStore(Protocol):
    async def append(key: SessionKey, entries: list[SessionStoreEntry]) -> None  # REQUIRED
    async def load(key: SessionKey) -> list[SessionStoreEntry] | None            # REQUIRED
    async def list_sessions(project_key: str) -> list[...]                       # OPTIONAL
    async def list_session_summaries(project_key) -> list[...]                   # OPTIONAL
    async def delete(key: SessionKey) -> None                                    # OPTIONAL
    async def list_subkeys(key: SessionListSubkeysKey) -> list[str]              # OPTIONAL
```

**`SessionKey`** (`types.py:1275-1295`):
```python
{ "project_key": str,         # sanitized cwd path
  "session_id": str,          # UUID
  "subpath": NotRequired[str] # sub-agent transcripts: "agents/{agent_id}/{nested_session_id}"
}
```

**`SessionStoreEntry`** fields: `uuid` (idempotency key), `type` (`user|assistant|system|summary|title|tag|mode_marker`), `message` (raw Anthropic API message), `session_id`, `parent_uuid`, `timestamp` (ISO 8601).

**Transcript mirror batcher constants** (`transcript_mirror_batcher.py:28-29`, KNOWN):
```
MAX_PENDING_ENTRIES = 500
MAX_PENDING_BYTES   = 1 MiB
SEND_TIMEOUT_SECONDS = 60.0
MIRROR_APPEND_MAX_ATTEMPTS = 3
MIRROR_APPEND_BACKOFF_S = (0.2, 0.8)
```

**Batcher correctness guarantees** (`transcript_mirror_batcher.py:99-148`, KNOWN):
1. **Detach-before-lock**: snapshot pending buffer before acquiring the write lock; producer can keep filling a fresh buffer while a flush is in flight. No head-of-line blocking.
2. **Coalesce by file_path**: one `store.append()` per session per flush cycle, not one per frame.
3. **Errors after lock release**: slow user callbacks cannot block subsequent drains.
4. **Shielded close flush** (`anyio.CancelScope(shield=True)`): the final batch still reaches the store under a cancelled scope (Ctrl+C / disconnect). Never raises — session mirror write errors must not crash the agent.
5. **Timeouts are NOT retried** (only 3-attempt backoff for non-timeout errors): "the in-flight call may still land after cancellation — a retry would launch a concurrent duplicate."

**`fold_session_summary`** (`types.py:1444-1462`, KNOWN): pure function that updates a session summary from a batch of new entries. Production SessionStore backends should wrap the read-fold-write in a transaction or per-session lock to prevent concurrent summary corruption.

**Session fork mechanic** (`session_mutations.py:430-555`, KNOWN): fork = copy the main-chain transcript up to message N, mint a fresh UUID for every entry, rewrite `parentUuid` through the old→new map. Sidechains (sub-agent sessions) are excluded. Progress entries are mapped (to resolve parent links) but not written to the fork. Result: a self-consistent independent conversation tree in a new `{forked_id}.jsonl`.

**Lancelot relevance:** Lancelot needs a durable session/checkpoint seam. The SessionStore pattern is directly copyable: two-method required protocol (`append`/`load`), `--session-mirror`-style transcript streaming, the batcher's detach-before-lock + coalesce discipline. The `SessionKey.subpath` pattern (sub-agent sessions nested under `agents/{agent_id}/`) maps to Lancelot's `agent_id` ContextVar. `session_mutations.py`'s fork UUID-remap is the concrete implementation of "branch at message N".

---

## Mechanism 6: Subprocess Lifecycle (5+5+kill Shutdown)

**Discovery order** (`subprocess_cli.py:81-127`, KNOWN):
```
1. _bundled/claude       (shipped in wheel — pip alone is sufficient)
2. shutil.which("claude")
3. ~/.npm-global/bin/claude
4. /usr/local/bin/claude
5. ~/.local/bin/claude
6. ~/node_modules/.bin/claude
7. ~/.yarn/bin/claude
8. ~/.claude/local/claude
```

**Shutdown sequence** (`subprocess_cli.py:545-601`, KNOWN):
```
1. Cancel stderr reader task
2. Under write lock: set _ready=False, close stdin  ← EOF signals CLI to finish flushing
3. Close stderr stream
4. try anyio.fail_after(5): await process.wait()    ← 5s grace for session-file flush
   except TimeoutError:
     process.terminate()  # SIGTERM
     try anyio.fail_after(5): await process.wait()
     except TimeoutError:
       process.kill()     # SIGKILL
       await process.wait()
5. Remove from _ACTIVE_CHILDREN module-level set
```

**Why the 5s grace** (`issue #625`): closing stdin signals the CLI to finish writing its JSONL transcript. SIGTERM before the file flush truncates it; the last assistant message is lost. 5s is the empirical window for the CLI to complete its write.

**Write-lock TOCTOU guard** (`subprocess_cli.py:559-560`): `_ready = False` is set **inside** the `_write_lock`, not before acquiring it. Without this: a concurrent `write()` could check `_ready=True`, get preempted, then `close()` sets `_ready=False` and closes stdin, then `write()` resumes and writes to a closed stream → `BrokenPipeError`.

**atexit cleanup** (`subprocess_cli.py:43`): `atexit.register(_kill_active_children)` → SIGTERM all tracked subprocesses on Python exit. Prevents zombie processes when the parent interpreter crashes.

**Version gate** (`subprocess_cli.py:717-750`): spawns `claude -v` with a **2-second timeout**, extracts `\d+\.\d+\.\d+`, warns if below `MINIMUM_CLAUDE_CODE_VERSION = "2.0.0"`. Non-blocking (warns, doesn't raise). Skip with `CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK=1`.

**Lancelot relevance:** Lancelot owns the serve loop and SIGTERM handling already (graceful drain, `LANCELOT_DRAIN_SECS`). The write-lock TOCTOU pattern and the atexit cleanup for zombie prevention are worth checking against any Lancelot subprocess/MCP server spawning.

---

## Mechanism 7: Speculative JSON Parsing

**The problem** (`subprocess_cli.py:640`, KNOWN): `anyio.TextReceiveStream` can truncate long JSON objects across multiple reads. The CLI also occasionally emits non-JSON debug lines (`[SandboxDebug]`).

**Algorithm** (exact, `subprocess_cli.py:640-686`, KNOWN):
```python
json_buffer = ""
for line in stdout_stream:
    for json_line in line.strip().split("\n"):  # multi-line chunks on Windows
        if not json_buffer and not json_line.startswith("{"):
            continue  # skip stray non-JSON lines (#347 fix)
        json_buffer += json_line
        if len(json_buffer) > _DEFAULT_MAX_BUFFER_SIZE:  # 1 MiB = 1024*1024
            json_buffer = ""; raise SDKJSONDecodeError(...)
        try:
            data = json.loads(json_buffer)
            json_buffer = ""; yield data
        except json.JSONDecodeError:
            continue  # keep accumulating until parseable
```

**Non-obvious behaviors:**
- Stray lines skip ONLY when `json_buffer` is empty. Mid-parse, a stray line appended corrupts the buffer.
- Buffer cap is 1 MiB. Exceeded → reset buffer + raise. **No partial recovery** — the message is gone.
- Multi-line split on `\n` even when using line-mode stream — handles Windows CR/LF and burst writes.

**Lancelot relevance:** Lancelot uses JSONL (tool results, MCP messages). The speculative-parse pattern (accumulate + json.loads retry + 1 MiB cap + skip non-`{` when idle) is a copyable robust JSONL reader for any stdio-backed MCP server transport.

---

## Mechanism 8: Settings Cascade + Provenance

**5-tier settings precedence** (high to low, `sdk.d.ts:1158+`, KNOWN):
```
managedSettings (enterprise admin, restrictive-only — cannot be widened by embedding app)
flag settings   (applyFlagSettings() / --settings inline)
user settings   (~/.claude/settings.json)
project settings (.claude/settings.json)
local settings  (.claude/settings.local.json)
```

**7 policy origins** (`sdk.d.ts:1941`, KNOWN):
```
'helper'   — admin policyHelper subprocess
'remote'   — /api/claude_code/settings (cached ~/.claude/remote-settings.json)
'plist'    — macOS MDM (Jamf/Intune)
'hklm'     — Windows HKEY_LOCAL_MACHINE
'file'     — managed-settings.json on disk
'parent'   — embedding app's managedSettings option
'hkcu'     — Windows HKEY_CURRENT_USER
```

**`applyFlagSettings` semantics** (`sdk.d.ts:2094-2113`, KNOWN): mid-session settings merge. Successive calls **shallow-merge top-level keys** — a second call with `{permissions: {...}}` replaces the entire `permissions` object. Pass `null` for a key to clear it from the flag layer. `undefined` is dropped by JSON serialization (no effect).

**`resolveSettings()` returns provenance per key** — which tier and origin contributed each setting. This enables debuggable policy introspection.

**Restrictive-only filter on `managedSettings`** (`sdk.d.ts:1664-1687`, KNOWN): permissive arrays (`permissions.allow`, `additionalDirectories`, `allowedMcpServers`) are **silently dropped**. Only deny/lock operations pass. Embedding apps can tighten but cannot loosen admin policy.

**Lancelot relevance:** Lancelot's `lancelot.secrets` + per-route `settings` maps to the flag layer. A 5-tier cascade is probably overkill for Lancelot's agent layer (3 tiers: operator / project / session is sufficient). The restrictive-only filter for enterprise embedding is worth having if Lancelot targets regulated-industry operators.

---

## Mechanism 9: Effort Levels + Thinking Config

**Effort levels** (`sdk.d.ts:480, 1442-1454`, KNOWN):
```
'low'    — minimal thinking, fastest
'medium' — moderate
'high'   — deep reasoning (default)
'xhigh'  — Opus 4.7 ONLY
'max'    — Claude 4.6+ family
```

`AgentDefinition.effort` also accepts a number (likely 0=low ... 4=max) for programmatic interpolation.

**ThinkingConfig** (`types.py:1554-1574`, KNOWN):
```python
ThinkingConfigAdaptive  = {"type": "adaptive", "display": "summarized"|"omitted"}
ThinkingConfigEnabled   = {"type": "enabled",  "budget_tokens": int, "display": ...}
ThinkingConfigDisabled  = {"type": "disabled"}
```

`display: "omitted"` — only the signature hash is returned, no thinking text. **Opus 4.7+ defaults to "omitted"** (lowers context pressure from thinking blocks).

**Lancelot relevance:** TransportPool already handles model selection. These types should be passed through as-is to the API. The `effort`→integer mapping is useful for programmatic sub-agent spawning that graded effort by task complexity.

---

## Mechanism 10: `backgroundTasks()` and `seedReadState()` (Novel Query Methods)

**`backgroundTasks(toolUseId?)` docstring** (`sdk.d.ts:2257-2269`, KNOWN):
> Background in-flight foreground tasks (Bash commands and subagents). With `toolUseId`, targets the single task started by that tool_use block; without it, backgrounds all foreground tasks — equivalent to pressing Ctrl+B. Each blocking tool call returns immediately with a "running in the background" tool_result and the turn continues; the task keeps running and emits a `task_notification` when it settles.
> Returns: `true` when at least one task was backgrounded; `false` only when `toolUseId` matched no foreground task.

This is how long-running tool calls (Bash, sub-agents) are made non-blocking mid-turn.

**`seedReadState(path, mtime)` docstring** (`sdk.d.ts:2192-2201`, KNOWN):
> Seed the CLI's readFileState cache with a path+mtime entry. Use when the client observed a Read that has since been removed from context (e.g. by snip), so a subsequent Edit won't fail "file not read yet". If the file changed on disk since the given mtime, the seed is skipped and Edit will correctly require a fresh Read.

This surfaces the TOCTOU protection mechanism in the CLI's file-access state machine.

**Lancelot relevance:** `backgroundTasks()` maps to Lancelot's async context — a Lancelot agent could expose a `background_task()` method that yields a non-blocking continuation token. `seedReadState` is a CLI-specific file-read cache mechanism; not directly applicable unless Lancelot implements similar file-access state tracking.

---

## Honest Anti-Patterns to Avoid

**1. Subprocess-delegated agent loop** (`CLAUDE_AGENT_SDK.md:Summary`, KNOWN): single-vendor lock (Anthropic CLI only), binary black box, no multi-provider. Not replicable for a general framework. Lancelot must own the loop.

**2. Python SDK as canonical reference** (`§C27`, KNOWN): the TypeScript SDK is the canonical surface (29 events vs 10, 23 Query methods vs 6, 60 options vs 30, 7 API providers vs 3). If implementing hooks, target the 29-event TS SDK list.

**3. Synchronous-per-frame SessionStore writes** (`transcript_mirror_batcher.py`, KNOWN): a naive impl that calls `store.append()` per frame will block the message stream, issue one round-trip per frame, and drop the final turn on cancellation. Must use the batcher's detach-before-lock + coalesce + shielded-close pattern.

**4. Missing the write-lock TOCTOU on stdin close** (`subprocess_cli.py:559`, KNOWN): `_ready = False` must be set inside the write lock. Without it, concurrent writes to a closing stdin produce `BrokenPipeError`.

**5. Skipping the 5-second stdin-EOF grace on shutdown** (`issue #625`, KNOWN): closing stdin without the grace truncates the CLI's session-file write. The last assistant message is lost. The grace period is not optional for subprocess-based architectures.

**6. Resource-link MCP content blocks to flat string** (`query.py:716-718`, KNOWN): Lancelot's `lancelot.mcp.mount` should handle `resource_link` content blocks the same way — flatten to `name\nuri\ndescription`. Silently drop binary `resource` blocks.

---

## API Shape Notes Worth Stealing

- **`@tool("greet", "desc", {"name": str})` + `create_sdk_mcp_server()`** — clean decorator-to-server ergonomic. `@app.tool` in Lancelot already copies this spirit.
- **`HookMatcher` with `matcher` field** — hooks can be registered with a glob/regex matcher for tool names; only matching tool calls invoke the hook. Avoids per-call overhead for narrow hooks.
- **`options.skills = ["search", "deploy"]` → `Skill(search), Skill(deploy)` in allowed_tools** — namespaced capability bundles via tool-name patterns.
- **`include_hook_events=True`** → hook events as messages in the stream. Zero-config observability without implementing callbacks.
- **`output_format: {"type": "json_schema", "schema": {...}}`** → structured output at the agent level (not per-tool). Maps to Lancelot's `@app.get(..., response_model=...)` but at the agent/turn level.
- **`persistSession: bool`** — disable JSONL writes for ephemeral/test runs. Useful for Lancelot's test harness.
- **`forwardSubagentText: bool`** — whether sub-agent turn text is forwarded to the parent stream. Controls sub-agent verbosity.
- **`agentProgressSummaries: bool`** — 30-second rolling summaries from long-running sub-agents. Prevents silent-for-30s sub-agents from looking hung.
- **`toolAliases: Record<string, string>`** — remap model-emitted tool names to resolved names (e.g. `{Bash: 'mcp__workspace__bash'}`). Enables tool-name migration without model retraining.

---

## Deprecations / Tactical Hacks to Skip

- **`max_thinking_tokens: int`** — deprecated; use `thinking: ThinkingConfig`. Will churn as thinking API evolves.
- **`mcp.Server.run(read_stream, write_stream)` for in-process routing** — the manual JSONRPC dispatch in `_handle_sdk_mcp_request` is a temporary shim. When the Python MCP SDK gains a Transport abstraction, this will be replaced. Don't copy the manual dispatch; wait for the Transport API.
- **`_simple_hash` (32-bit JS-compatible string hash)** — needed only for matching the CLI's Bun-hashed project directory names. Framework-specific; not a general primitive.
- **The bundled Bun binary distribution** — packaging a 50 MB binary inside a pip wheel is specific to the CLI-delegation architecture. Not applicable to Lancelot's direct-loop design.
- **`betas: SdkBeta[]` currently = one value** (`'context-1m-2025-08-07'`). Tactical; the 1M-context window will become standard. Don't build special logic around it.
