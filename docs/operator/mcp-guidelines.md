# MCP Tool Design — Minimal-Return Discipline

> **Cardinal rule for MCP tools:** *every byte your tool returns lives in the agent's context window until the conversation ends.* Keep returns tiny by default; give the agent a handle and a separate `fetch_details` tool when it needs more.

This is Dex Horthy's (HumanLayer) often-restated point:

> "MCP tools that return big blobs of JSON, that's going to flood your context window with nonsense."

It's the most expensive mistake AI-app developers make on Lancelot's MCP adapter — and it's silent: your tests pass, your app works, you just burn a lot of tokens and run into context-window limits sooner.

This doc is the **byte-path discipline**. Lancelot ships the primitives; you ship the design.

## Why returns must be small

| Anti-pattern | Cost | Symptom |
|---|---|---|
| Return the full DB row (50+ fields, 4-10 KB) | ~1k tokens/call × N tool calls → context cliff | Agent "forgets" earlier turns; eval scores degrade silently |
| Return a JSON document with HTML/markdown in fields | ~2-5k tokens per call | Same as above + parsing fragility |
| Return an *array* of records when only one is needed | O(N) blow-up | Worst case: 50-100 KB per call |
| Return a full LLM transcript / agent log | Re-feeding the agent its own output | Recursive context inflation |

**The framework warns when you cross the threshold.** Lancelot's MCP adapter logs a `WARNING` whenever a tool's serialized return exceeds `max_response_bytes` (default **4 KB**). The tool still runs — Lancelot will not truncate your data — but the log is your signal to redesign:

```
WARNING lancelot.mcp: MCP tool 'list_orders' returned 18432 bytes (> 4096).
Big tool returns flood the agent's context window; consider returning an
id / handle and exposing a separate `fetch_details` tool.
```

You see this warning during dev (or in the operator dashboard if you wire it into your alerting). Treat it like a slow-test warning: keep the green status until you've actively decided "no, this big return is genuinely needed."

## The handle-and-fetch pattern (recommended)

Instead of:

```python
@app.tool(description="List recent orders for a customer")
def list_orders(customer_id: str) -> list[dict]:
    return db.query("SELECT * FROM orders WHERE customer_id = ?", customer_id)
    # → returns 50 rows × 30 cols → ~30 KB → WARN
```

Do:

```python
@app.tool(description="List recent order IDs for a customer")
def list_orders(customer_id: str) -> list[str]:
    rows = db.query("SELECT id FROM orders WHERE customer_id = ? "
                    "ORDER BY created_at DESC LIMIT 20", customer_id)
    return [r["id"] for r in rows]
    # → returns 20 strings → ~500 B → silent (good)

@app.tool(description="Fetch one order's full details by id")
def get_order(order_id: str) -> dict:
    return db.query_one("SELECT * FROM orders WHERE id = ?", order_id)
    # → 1 KB per call but agent calls it only for the row(s) it cares about
```

This gives the agent the *exact same information access* — it can still reach every field of every order — but the agent picks which orders to expand, paying only for the ones that matter. The "list" tool's context cost is now O(1) per call, not O(N × fields).

## When a big return is actually right

There are 3 cases where you correctly suppress the warning:

1. **Schema introspection.** A tool that returns the available enum values, JSON-schema fragments, or column names — the agent needs the full schema to construct subsequent calls correctly. Schema info usually compresses well in the model's representation; the token-per-byte cost is lower.

2. **A genuinely small dataset.** If your "orders" table only has 20 rows ever, the warning is mistargeted. Suppress it with `lancelot.mcp.mount(app, max_response_bytes=0)`.

3. **Final-answer payload.** If the agent has explicitly requested the full data because the user asked for it (e.g. "show me all the columns"), the warning is correct but the cost is justified.

For case 1 and 2, set `max_response_bytes=0` to disable the threshold globally. For case 3, leave the warn on and accept the noise — it's a real signal everywhere else.

## Configuring the threshold

```python
import lancelot
import lancelot.mcp

app = lancelot.App()

# Default: warn above 4 KB
lancelot.mcp.mount(app)

# Stricter: warn above 1 KB (recommended for token-conscious apps)
lancelot.mcp.mount(app, max_response_bytes=1024)

# Disable the warn entirely
lancelot.mcp.mount(app, max_response_bytes=0)
```

The warning logs to the `lancelot.mcp` logger (stdlib `logging`). Hook it into your operator pipeline:

```python
import logging
logging.getLogger("lancelot.mcp").setLevel(logging.WARNING)
# ... your handler / formatter ...
```

For production, wire `lancelot.mcp` logs into your structured-log sink so the warning shows up in dashboards alongside RED + queue-wait metrics (the M7 observability stack).

## Schema-strict input is also part of the discipline

The corollary to "minimal output" is **strict input**. Lancelot auto-derives an `input_schema` for every `@app.tool` from your function's type hints — and the MCP adapter passes this schema to the client. Agents (Claude, Cursor, etc.) use the schema to decide which arguments are valid; a stricter schema means fewer wasted retries.

✅ Use specific types where you mean them:

```python
@app.tool
def fetch(order_id: str, *, fields: list[str] | None = None) -> dict:
    """Fetch one order; pass `fields` to limit the response."""
```

❌ Avoid `Any` / unconstrained `dict`:

```python
@app.tool
def fetch(args: dict) -> dict:  # the agent has no clue what to send
```

Lancelot's Pydantic v2 schema generation surfaces JSON-Schema constraints (`enum`, `pattern`, `minLength`, etc.) — use them. The agent reads them, validates upstream of the call, and you skip a round-trip when the input would have failed validation.

## See also

- `docs/agents.md` — Lancelot conventions for LLM-driven code agents
- `docs/operator/runbook.md` — the operator's runbook (RED + TTFT + queue-wait dashboards)
- `lancelot/mcp.py` — the implementation; spec compliance notes in the module docstring
- [MCP spec](https://modelcontextprotocol.io/specification/2025-06-18) — the wire protocol Lancelot speaks
