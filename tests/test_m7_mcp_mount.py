"""M7 T1.4 + T1.5 — lancelot.mcp.mount() convenience + minimal-return warn.

T1.4: `lancelot.mcp.mount(app)` is the canonical top-level entry point
      (Claude Code / Cursor agents discover this when asked "expose
      lancelot tools as MCP"). Identical effect to `app.mcp(path)`.

T1.5: tools that return > 4 KB log a warning so the developer notices —
      "MCP tools that return big blobs of JSON flood your context window."
      Does NOT truncate (user might want a big blob); just logs.
"""
import json
import logging

from pydantic import BaseModel

import lancelot
import lancelot.mcp
from lancelot.testclient import TestClient


# ----- T1.4 mount() top-level convenience ---------------------------------

def test_t1_4_mount_creates_mcp_endpoint():
    """`lancelot.mcp.mount(app)` registers POST /mcp; initialize +
    tools/list + tools/call all work end-to-end."""
    app = lancelot.App()

    @app.tool(description="Say hi to someone")
    def greet(name: str) -> str:
        return f"hello, {name}"

    lancelot.mcp.mount(app)

    with TestClient(app) as client:
        # 1. initialize
        r = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.status_code == 200
        body = r.json()
        assert body["result"]["protocolVersion"] == lancelot.mcp.PROTOCOL_VERSION
        # session id minted
        assert r.headers.get("mcp-session-id"), "expected mcp-session-id header"

        # 2. tools/list
        r = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert r.status_code == 200
        tools = r.json()["result"]["tools"]
        assert any(t["name"] == "greet" for t in tools)

        # 3. tools/call — note the tool param IS literally called `name`
        # (collides with App.call_tool's signature without PEP 570 fix).
        r = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 3,
            "method": "tools/call",
            "params": {"name": "greet",
                       "arguments": {"name": "Lancelot"}}})
        assert r.status_code == 200
        result = r.json()["result"]
        # Tool returns JSON-serialized into MCP text content.
        text = result["content"][0]["text"]
        assert json.loads(text) == "hello, Lancelot"


def test_t1_4_mount_custom_path():
    """`lancelot.mcp.mount(app, path='/api/mcp')` works at non-default
    path too."""
    app = lancelot.App()

    @app.tool(description="ping")
    def ping() -> str:
        return "pong"

    lancelot.mcp.mount(app, path="/api/mcp")

    with TestClient(app) as client:
        r = client.post("/api/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 200
        assert "ping" in {t["name"] for t in r.json()["result"]["tools"]}


# ----- T1.5 minimal-return discipline (warn on big return) ----------------

def test_t1_5_warns_when_tool_returns_above_threshold(caplog):
    """Returning more than `max_response_bytes` triggers a WARNING log
    so the developer sees their tool's context-window impact."""
    app = lancelot.App()

    @app.tool(description="returns a big blob")
    def big() -> dict:
        return {"data": "x" * 5000}  # JSON-serialized > 4 KB

    lancelot.mcp.mount(app, max_response_bytes=4096)

    with TestClient(app) as client:
        with caplog.at_level(logging.WARNING, logger="lancelot.mcp"):
            r = client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"name": "big", "arguments": {}}})
            assert r.status_code == 200
            # Tool still runs successfully — we warn, don't truncate.
            text = r.json()["result"]["content"][0]["text"]
            assert json.loads(text)["data"].startswith("xxx")

        # Warning was logged.
        assert any("flood the agent's context window" in rec.message
                   for rec in caplog.records), \
            f"expected warn, got: {[r.message for r in caplog.records]}"


def test_t1_5_silent_below_threshold(caplog):
    """Small returns produce no warning (the common case)."""
    app = lancelot.App()

    @app.tool(description="small return")
    def small() -> str:
        return "ok"

    lancelot.mcp.mount(app, max_response_bytes=4096)

    with TestClient(app) as client:
        with caplog.at_level(logging.WARNING, logger="lancelot.mcp"):
            r = client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"name": "small", "arguments": {}}})
            assert r.status_code == 200

        # No warning.
        assert not any("flood" in rec.message for rec in caplog.records)


def test_t1_5_max_response_bytes_zero_disables_warn(caplog):
    """`max_response_bytes=0` opts out of the warning entirely."""
    app = lancelot.App()

    @app.tool(description="huge")
    def huge() -> dict:
        return {"data": "x" * 100000}

    lancelot.mcp.mount(app, max_response_bytes=0)

    with TestClient(app) as client:
        with caplog.at_level(logging.WARNING, logger="lancelot.mcp"):
            r = client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"name": "huge", "arguments": {}}})
            assert r.status_code == 200

        assert not any("flood" in rec.message for rec in caplog.records)
