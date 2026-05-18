from lancelot import _native


class Lancelot:
    def __init__(self):
        self._routes = []  # (method, path, handler)

    def _add(self, method, path):
        def deco(fn):
            self._routes.append((method, path, fn))
            return fn

        return deco

    def post(self, path):
        return self._add("POST", path)

    def get(self, path):
        return self._add("GET", path)


def run(app: "Lancelot", host: str = "127.0.0.1", port: int = 8000):
    _native.run(app._routes, host, port)
