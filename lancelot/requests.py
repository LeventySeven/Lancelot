from dataclasses import dataclass


@dataclass
class Request:
    method: str
    path: str
    query_params: dict
    headers: dict
    cookies: dict
    path_params: dict
    body: bytes
    app: object
    _json_cache: object = None

    @property
    def state(self):
        return getattr(self.app, "state", None)
