"""M2-P5 — `@app.tool` produces a JSON-Schema tool descriptor reusing
the existing Pydantic/Dependant compiler. SAME code path that emits
OpenAPI also emits the MCP tool schema. Replaces hand-rolled per-tool
JSON-Schema dicts (Embedder 7 tools, Cursor 80, Codex 20, Claude 21,
Stagehand 18 — 10+ products)."""


def test_tool_decorator_emits_anthropic_shape_schema():
    """`@app.tool` registers a callable with auto-generated input_schema
    from type hints. The schema is Anthropic-tool-format compatible (the
    wire shape every provider's conversion ends up at): {name, description,
    input_schema: {type:'object', properties:{}, required:[]}}."""
    import lancelot

    app = lancelot.App()

    @app.tool(description="Search the web for a query")
    def web_search(q: str, k: int = 5) -> list[str]:
        """Return up to k results for q."""
        return [f"r{i}" for i in range(k)]

    schemas = app.tool_schemas()
    assert len(schemas) == 1
    s = schemas[0]
    assert s["name"] == "web_search"
    assert s["description"] == "Search the web for a query"
    assert s["input_schema"]["type"] == "object"
    assert "q" in s["input_schema"]["properties"]
    assert "k" in s["input_schema"]["properties"]
    assert s["input_schema"]["required"] == ["q"]


def test_tool_handler_runs():
    """The tool callable is invokable via app.call_tool(name, **kwargs).
    Reuses lancelot's existing Pydantic validation for the inputs."""
    import lancelot

    app = lancelot.App()

    @app.tool(description="add two ints")
    def add(a: int, b: int) -> int:
        return a + b

    result = app.call_tool("add", a=2, b=3)
    assert result == 5


def test_tool_call_validates_input():
    """A wrong-typed input raises a real validation error — pipes
    through Pydantic just like an HTTP route."""
    import lancelot
    import pytest

    app = lancelot.App()

    @app.tool()
    def square(n: int) -> int:
        return n * n

    with pytest.raises(Exception):  # pydantic ValidationError shape
        app.call_tool("square", n="not-an-int")
