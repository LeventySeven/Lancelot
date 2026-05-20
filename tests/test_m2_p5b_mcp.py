"""M2-P5b — `App.mcp()` mounts a Streamable-HTTP MCP server over the
existing `@app.tool` registry. The 5 gaps Robyn's mcp.py left open are
closed here: Mcp-Session-Id roundtrip, MCP-Protocol-Version 400-on-
unsupported, Origin allow-list. (Resumption via Last-Event-ID is OUT —
matches our strict-synthesis cut of `resumable=` on @app.stream.)"""
import json


def test_mcp_initialize_returns_capabilities_and_session_id():
    import lancelot
    from lancelot.testclient import TestClient

    app = lancelot.App()

    @app.tool(description="add two ints")
    def add(a: int, b: int) -> int:
        return a + b

    app.mcp(path="/mcp", allowed_origins=["https://claude.ai"])

    with TestClient(app) as c:
        r = c.post("/mcp",
                   headers={"origin": "https://claude.ai",
                            "mcp-protocol-version": "2025-06-18"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {}})
        assert r.status_code == 200
        body = r.json()
        assert body["jsonrpc"] == "2.0" and body["id"] == 1
        assert "result" in body
        assert body["result"]["protocolVersion"] == "2025-06-18"
        assert "tools" in body["result"]["capabilities"]
        assert "mcp-session-id" in r.headers


def test_mcp_tools_list_returns_app_tool_registry():
    import lancelot
    from lancelot.testclient import TestClient

    app = lancelot.App()

    @app.tool(description="search the web")
    def web_search(q: str, k: int = 5) -> list[str]:
        return []

    app.mcp(path="/mcp")

    with TestClient(app) as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                 "method": "tools/list"})
        assert r.status_code == 200
        tools = r.json()["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "web_search"
        assert "q" in tools[0]["inputSchema"]["properties"]


def test_mcp_tools_call_invokes_tool_and_validates_inputs():
    import lancelot
    from lancelot.testclient import TestClient

    app = lancelot.App()

    @app.tool(description="add")
    def add(a: int, b: int) -> int:
        return a + b

    app.mcp(path="/mcp")

    with TestClient(app) as c:
        r = c.post("/mcp", json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 3}}})
        assert r.status_code == 200
        result = r.json()["result"]
        # MCP tool call result: content list with text item
        assert result["content"][0]["type"] == "text"
        assert json.loads(result["content"][0]["text"]) == 5


def test_mcp_origin_validation_rejects_unlisted_origin():
    import lancelot
    from lancelot.testclient import TestClient

    app = lancelot.App()
    app.mcp(path="/mcp", allowed_origins=["https://claude.ai"])

    with TestClient(app) as c:
        r = c.post("/mcp",
                   headers={"origin": "https://attacker.evil"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.status_code == 403


def test_mcp_protocol_version_unsupported_400():
    import lancelot
    from lancelot.testclient import TestClient

    app = lancelot.App()
    app.mcp(path="/mcp")

    with TestClient(app) as c:
        r = c.post("/mcp", headers={"mcp-protocol-version": "1999-01-01"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.status_code == 400
