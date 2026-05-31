"""M15 T2 (#11) — honor operation_id / generate_unique_id_function /
openapi_extra in OpenAPI generation (dead-code bug).

`route.operation_id` was stored on the Route but never consumed;
`generate_unique_id_function` was swallowed into `_extra_kwargs` and ignored;
`openapi_extra` had nowhere to land. build_openapi always used the hardcoded
`operation_id(r)` formula. The operationId IS the generated SDK / MCP method
name, so this is the AI/SDK-codegen audience's primary lever — pure
/openapi.json bytes, built once + memoized (zero hot-path cost).
"""
import lancelot
from lancelot.openapi import operation_id as _formula


def _op(app, path, method="get"):
    return app.openapi()["paths"][path][method]


def test_operation_id_verbatim():
    app = lancelot.App()

    @app.get("/checkouts", operation_id="checkouts:create")
    async def create_checkout():
        return {}

    op = _op(app, "/checkouts")
    assert op["operationId"] == "checkouts:create"
    # NOT the hardcoded formula (which would be create_checkout_checkouts_get).
    assert op["operationId"] != "create_checkout_checkouts_get"


def test_generate_unique_id_function():
    app = lancelot.App(generate_unique_id_function=lambda r: f"x_{r.name}")

    @app.get("/a")
    async def alpha():
        return {}

    @app.post("/b")
    async def beta():
        return {}

    assert _op(app, "/a")["operationId"] == "x_alpha"
    assert _op(app, "/b", "post")["operationId"] == "x_beta"


def test_operation_id_precedence():
    """Per-route operation_id beats the app-level factory beats the formula."""
    app = lancelot.App(generate_unique_id_function=lambda r: f"factory_{r.name}")

    @app.get("/explicit", operation_id="verbatim_wins")
    async def explicit():
        return {}

    @app.get("/from_factory")
    async def from_factory():
        return {}

    # Explicit per-route wins over the factory.
    assert _op(app, "/explicit")["operationId"] == "verbatim_wins"
    # No per-route id → the factory is used (not the formula).
    assert _op(app, "/from_factory")["operationId"] == "factory_from_factory"


def test_openapi_extra_merged():
    app = lancelot.App()

    @app.get("/x", openapi_extra={"x-speakeasy-name-override": "list"})
    async def x():
        return {}

    op = _op(app, "/x")
    assert op.get("x-speakeasy-name-override") == "list"
    # The generated keys are still present (extra is a shallow merge, not a
    # replacement).
    assert "operationId" in op
    assert "responses" in op


def test_openapi_extra_can_override_generated_key():
    """openapi_extra is merged AFTER the generated keys, so a user can override
    a generated value (e.g. supply their own summary)."""
    app = lancelot.App()

    @app.get("/y", openapi_extra={"summary": "Custom Summary"})
    async def y():
        return {}

    assert _op(app, "/y")["summary"] == "Custom Summary"


def test_default_operation_id_still_formula():
    """No overrides → the existing MUST-32 formula is unchanged (byte-parity)."""
    app = lancelot.App()

    @app.get("/items")
    async def list_items():
        return {}

    route = app.routes[-1]
    assert _op(app, "/items")["operationId"] == _formula(route)
