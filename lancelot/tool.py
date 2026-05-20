"""M2-P5 — `@app.tool` decorator. The single biggest LLM-tool ergonomics
win that recurs across 10+ real products (Cursor 80 tools, Codex 20,
Claude 21, Stagehand 18, Embedder 7, every MCP server). Lancelot's
existing Pydantic/Dependant compiler ALREADY produces a JSON-Schema
from type hints; `@app.tool` just exposes it under the
Anthropic-tool-format shape (which every provider's conversion ends up at).

A `@app.tool` is NOT an HTTP route — it's a callable + a schema. Same
function can also be `@app.post` if you want HTTP exposure.

Surface (≤6 lines, FastAPI-`BackgroundTasks`-shape):
    @app.tool(description='Search', read_only=True)
    def search(q: str, k: int = 5) -> list[Hit]: ...
    app.tool_schemas()          # list of {name, description, input_schema}
    app.call_tool('search', q='x')   # invoke with Pydantic-validated kw

MCP Streamable-HTTP server mount is a follow-up commit — the per-tool
schema produced here is exactly what MCP expects on the wire."""
import inspect as _inspect
import typing as _typing
from dataclasses import dataclass, field


@dataclass
class ToolSpec:
    name: str
    description: str
    func: object
    read_only: bool = False
    scopes: list = field(default_factory=list)
    input_schema: dict = field(default_factory=dict)
    # Tier-C.C3: output_schema auto-derived from the return-type annotation,
    # if present. Anthropic/OpenAI/Google all accept (and use) an optional
    # output schema; emitting one lets the model better-shape its calls.
    # When the function has no return annotation OR returns None, this
    # stays None (we do NOT make one up).
    output_schema: dict | None = None
    _validator: object = None  # Pydantic core_schema for arg validation


def _build_tool_schema(func, name: str, description: str) -> tuple[dict, object, dict | None]:
    """Build the {type:'object', properties, required} input schema +
    a Pydantic TypeAdapter that validates kwargs against the signature
    + (Tier-C.C3) an output_schema derived from the return-type
    annotation if one is present.

    Reuses the SAME machinery FastAPI/Lancelot use for HTTP request
    validation — single source of truth, no second compiler."""
    from pydantic import TypeAdapter

    sig = _inspect.signature(func)
    hints = _typing.get_type_hints(func, include_extras=True)
    props: dict = {}
    required: list[str] = []
    field_models: dict = {}
    for param_name, p in sig.parameters.items():
        ann = hints.get(param_name, p.annotation if p.annotation
                        is not _inspect.Parameter.empty else str)
        field_models[param_name] = (ann, p.default if p.default
                                    is not _inspect.Parameter.empty else ...)
        # Inline the JSON-Schema for the field via TypeAdapter
        adapter = TypeAdapter(ann)
        props[param_name] = adapter.json_schema()
        if p.default is _inspect.Parameter.empty:
            required.append(param_name)
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    # The validator: build a pydantic dataclass-style ad-hoc model for kwargs
    from pydantic import create_model

    pyd_fields = {n: (a, d) for n, (a, d) in field_models.items()}
    Model = create_model(f"_Tool_{name}_Args", **pyd_fields)

    # Tier-C.C3: derive output_schema from the return-type hint, if any.
    # Skipped (None) for: no annotation, `-> None` (no payload to schema),
    # or any annotation TypeAdapter refuses (e.g. bare `typing.Any`).
    output_schema: dict | None = None
    ret_ann = hints.get("return", _inspect.Signature.empty)
    if ret_ann is not _inspect.Signature.empty and ret_ann is not None \
            and ret_ann is not type(None):
        try:
            output_schema = TypeAdapter(ret_ann).json_schema()
        except Exception:  # noqa: BLE001 — keep the tool usable even if
            # the return type is unschemable (e.g. some abstract Protocol)
            output_schema = None
    return schema, Model, output_schema


def make_tool_spec(func, *, name: str | None = None,
                   description: str | None = None,
                   read_only: bool = False,
                   scopes: list | None = None) -> ToolSpec:
    name = name or func.__name__
    description = description or (func.__doc__ or "").strip().splitlines()[0] \
        if func.__doc__ else (description or "")
    schema, validator, output_schema = _build_tool_schema(
        func, name, description)
    return ToolSpec(name=name, description=description, func=func,
                    read_only=read_only, scopes=scopes or [],
                    input_schema=schema, output_schema=output_schema,
                    _validator=validator)
