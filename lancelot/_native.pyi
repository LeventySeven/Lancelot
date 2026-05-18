from typing import Any, Callable

class FnInfo:
    def __init__(
        self, handler: Callable[..., Any], is_async: bool, arity: int
    ) -> None: ...
    is_async: bool
    arity: int

class Request: ...

def run(
    routes: list[tuple[str, str, Callable[..., Any]]], host: str, port: int
) -> None: ...
def _ping() -> str: ...
