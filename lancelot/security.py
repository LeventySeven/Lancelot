"""FastAPI-surface security schemes. Extraction / 401 / WWW-Authenticate
are byte-faithful to research/deep/03 §1 (MUST-1..14). Scopes are PLUMBED,
not enforced (spec line 68 / MUST-13 — enforcement is M4). Full OAuth2
flows / OpenID Connect / HTTPDigest / scope-enforcement → P7 quarantine."""
from lancelot.exceptions import HTTPException


class SecurityBase:
    """Marker base: a callable dependency the DI solver invokes with the
    live Request (recognised via isinstance — di.py)."""

    model: dict
    scheme_name: str


class _APIKeyBase(SecurityBase):
    location = "header"

    def __init__(self, *, name, scheme_name=None, description=None, auto_error=True):
        self.name = name
        self.auto_error = auto_error
        self.scheme_name = scheme_name or self.__class__.__name__
        m = {"type": "apiKey", "in": self.location, "name": name}
        if description is not None:
            m["description"] = description
        self.model = m

    def _check(self, value):
        if not value:  # MUST-4: empty-string == missing
            if self.auto_error:
                raise HTTPException(401, "Not authenticated",
                                    {"WWW-Authenticate": "APIKey"})  # MUST-2
            return None
        return value


class APIKeyHeader(_APIKeyBase):
    location = "header"

    async def __call__(self, request):
        h = {k.lower(): v for k, v in request.headers.items()}
        return self._check(h.get(self.name.lower()))


class APIKeyQuery(_APIKeyBase):
    location = "query"

    async def __call__(self, request):
        return self._check(request.query_params.get(self.name))


class APIKeyCookie(_APIKeyBase):
    location = "cookie"

    async def __call__(self, request):
        return self._check(request.cookies.get(self.name))
