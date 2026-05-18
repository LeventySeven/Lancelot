from lancelot import App


def test_app_registers_methods_and_state():
    app = App()

    @app.get("/items/{item_id}")
    def read(item_id): ...

    @app.post("/items")
    def create(): ...

    routes = {(r.method, r.path) for r in app.routes}
    assert ("GET", "/items/{item_id}") in routes
    assert ("POST", "/items") in routes
    app.state.counter = 7
    assert app.state.counter == 7
