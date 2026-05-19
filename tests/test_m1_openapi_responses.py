from lancelot import App
from lancelot.openapi import responses_for, _HTTP_VALIDATION_ERROR, _VALIDATION_ERROR


def test_responses_success_and_422_injection():
    app = App()

    @app.get("/x/{i}")
    def x(i: int):
        return {}

    @app.get("/noparams")
    def n():
        return {}

    r1 = responses_for(app.routes[0])
    assert r1["200"]["description"] == "Successful Response"
    assert r1["200"]["content"]["application/json"]["schema"] == {}
    assert r1["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HTTPValidationError"}
    assert "422" not in responses_for(app.routes[1])  # no params/body (MUST-36)
    assert _VALIDATION_ERROR["title"] == "ValidationError"
    assert _VALIDATION_ERROR["required"] == ["loc", "msg", "type"]
    assert _HTTP_VALIDATION_ERROR["title"] == "HTTPValidationError"
