"""M2-P5b + M7 T1.4 — MCP Streamable-HTTP server mount.

M2-P5b shipped `app.mcp()` (method on App). M7 T1.4 adds:
  - `lancelot.mcp.mount(app, ...)` top-level convenience (so
    code-discovery agents see one canonical entry point)
  - `max_response_bytes` warn-knob (default 4096) — per T1.5 Dex Horthy
    "MCP tools that return big blobs of JSON flood your context window."
    Big tool returns log a warning so the developer notices.

The `@app.tool` registry (M2-P5) already produces JSON-Schema that's
wire-compatible with the MCP `tools/list` shape; this file is just the
JSON-RPC 2.0 routing on top + the 3 production-shape spec gaps Robyn's
`mcp.py` left open (research/deep/03 §1.5):

  - `Mcp-Session-Id` header issued on initialize, accepted thereafter
  - `MCP-Protocol-Version` validated; 400 on unsupported
  - `Origin` allow-list (defaults to wide-open; pass allowed_origins=
    to restrict — Claude Desktop etc. need this)

OUT-OF-SCOPE per strict synthesis: Last-Event-ID resumption (mirrors
the cut of `resumable=` on @app.stream — zero product citations).

Spec: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
"""
import logging
import secrets

PROTOCOL_VERSION = "2025-06-18"
_SERVER_INFO = {"name": "lancelot", "version": "0.0.0"}

# M7 T1.5 (Dex Horthy minimal-return discipline): default warn threshold.
# 4 KB is the rule-of-thumb above which a single tool return measurably
# inflates the agent's context window. Configurable; set 0 to disable.
DEFAULT_MAX_RESPONSE_BYTES = 4096

_log = logging.getLogger("lancelot.mcp")


def make_handler(app, *, allowed_origins=None,
                 max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES):
    """Return an async ASGI handler that processes one MCP JSON-RPC
    request. `app` is the Lancelot App (uses app._tools registry +
    app.call_tool). `allowed_origins=None` skips Origin validation
    (server-to-server); pass a list to enforce.

    M7 T1.5: `max_response_bytes` warns (via stdlib logging) when a
    tool's serialized return exceeds the threshold. Set 0 to disable.
    Does NOT truncate — the user might intentionally want a big blob.
    """
    sessions: set[str] = set()

    async def handler(request):
        # ASGI-level Origin validation (the MCP spec's anti-CSRF/DNS-
        # rebinding guard). Defaulting to None means no check (matches
        # plain HTTP); production deployments pass a list.
        if allowed_origins is not None:
            origin = request.headers.get("origin", "")
            if origin and origin not in allowed_origins:
                from lancelot.exceptions import HTTPException

                raise HTTPException(status_code=403,
                                    detail="Origin not allowed")
        ver = request.headers.get("mcp-protocol-version")
        if ver and ver != PROTOCOL_VERSION:
            from lancelot.exceptions import HTTPException

            raise HTTPException(status_code=400,
                                detail=f"Unsupported MCP-Protocol-Version: "
                                       f"{ver}")
        body = request.body
        import json as _json

        try:
            msg = _json.loads(body) if body else {}
        except _json.JSONDecodeError:
            return _err(None, -32700, "Parse error")
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "initialize":
            sid = secrets.token_hex(16)
            sessions.add(sid)
            from lancelot.responses import JSONResponse

            return JSONResponse(
                {"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": _SERVER_INFO,
                    "capabilities": {"tools": {}, "resources": {},
                                     "prompts": {}}}},
                headers={"mcp-session-id": sid})
        if method == "tools/list":
            tools = [{"name": s.name, "description": s.description,
                      "inputSchema": s.input_schema}
                     for s in app._tools.values()]
            return _ok(mid, {"tools": tools})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name not in app._tools:
                return _err(mid, -32602, f"Unknown tool: {name}")
            try:
                result = app.call_tool(name, **args)
            except Exception as exc:  # noqa: BLE001
                return _ok(mid, {"isError": True, "content": [
                    {"type": "text", "text": str(exc)}]})
            serialized = _json.dumps(result)
            # M7 T1.5: minimal-return discipline warn (Dex Horthy pattern).
            if max_response_bytes and len(serialized) > max_response_bytes:
                _log.warning(
                    "MCP tool '%s' returned %d bytes (> %d). "
                    "Big tool returns flood the agent's context window; "
                    "consider returning an id / handle and exposing a "
                    "separate `fetch_details` tool.",
                    name, len(serialized), max_response_bytes)
            return _ok(mid, {"content": [
                {"type": "text", "text": serialized}]})
        return _err(mid, -32601, f"Method not found: {method}")

    return handler


def mount(app, path: str = "/mcp", *, allowed_origins=None,
          max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES):
    """M7 T1.4: top-level convenience to mount an MCP Streamable-HTTP
    endpoint that auto-exposes the app's `@app.tool` registry.

    Identical effect to `app.mcp(path)` but importable as
    `lancelot.mcp.mount(app)` — the canonical entry point that
    code-discovery agents (Claude Code, Cursor) will surface when a
    user asks "how do I expose my Lancelot tools as MCP?".

    Args:
        app: a Lancelot App.
        path: HTTP path to mount (default `/mcp`).
        allowed_origins: list of allowed Origin headers, or None for
            no check (server-to-server). Browser-hosted clients
            (Claude Desktop / claude.ai) require this set.
        max_response_bytes: warn-threshold per T1.5; 0 disables.

    Returns: the same app (for chaining if desired).
    """
    from lancelot.requests import Request
    from lancelot.routing import Route

    h = make_handler(app, allowed_origins=allowed_origins,
                     max_response_bytes=max_response_bytes)

    async def _mcp_endpoint(request: "Request"):
        return await h(request)

    _mcp_endpoint.__annotations__ = {"request": Request}
    app.router.add(Route("POST", path, _mcp_endpoint,
                         include_in_schema=False))
    return app


__all__ = ["mount", "make_handler",
           "PROTOCOL_VERSION", "DEFAULT_MAX_RESPONSE_BYTES"]


def _ok(mid, result):
    from lancelot.responses import JSONResponse

    return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": result})


def _err(mid, code, message, data=None):
    from lancelot.responses import JSONResponse

    body = {"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}}
    if data is not None:
        body["error"]["data"] = data
    return JSONResponse(body)
