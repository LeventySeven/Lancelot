"""M2-P5b — MCP Streamable-HTTP server mount. The `@app.tool` registry
(M2-P5) already produces JSON-Schema that's wire-compatible with the
MCP `tools/list` shape; this file is just the JSON-RPC 2.0 routing on
top + the 3 production-shape spec gaps Robyn's `mcp.py` left open
(research/deep/03 §1.5):

  - `Mcp-Session-Id` header issued on initialize, accepted thereafter
  - `MCP-Protocol-Version` validated; 400 on unsupported
  - `Origin` allow-list (defaults to wide-open; pass allowed_origins=
    to restrict — Claude Desktop etc. need this)

OUT-OF-SCOPE per strict synthesis: Last-Event-ID resumption (mirrors
the cut of `resumable=` on @app.stream — zero product citations).

Spec: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
"""
import secrets

PROTOCOL_VERSION = "2025-06-18"
_SERVER_INFO = {"name": "lancelot", "version": "0.0.0"}


def make_handler(app, *, allowed_origins=None):
    """Return an async ASGI handler that processes one MCP JSON-RPC
    request. `app` is the Lancelot App (uses app._tools registry +
    app.call_tool). `allowed_origins=None` skips Origin validation
    (server-to-server); pass a list to enforce."""
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
            return _ok(mid, {"content": [
                {"type": "text", "text": _json.dumps(result)}]})
        return _err(mid, -32601, f"Method not found: {method}")

    return handler


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
