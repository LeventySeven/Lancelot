"""FastAPI-surface security schemes. Extraction / 401 / WWW-Authenticate
are byte-faithful to research/deep/03 §1 (MUST-1..14). Scopes are PLUMBED,
not enforced (spec line 68 / MUST-13 — enforcement is M4). Full OAuth2
flows / OpenID Connect / HTTPDigest / scope-enforcement → P7 quarantine."""
import base64
import binascii
from dataclasses import dataclass

from lancelot.exceptions import HTTPException


@dataclass
class HTTPAuthorizationCredentials:
    scheme: str
    credentials: str


@dataclass
class HTTPBasicCredentials:
    username: str
    password: str


def _scheme_param(value):
    # MUST-1: partition on the FIRST space; param stripped; missing -> ("","")
    if not value:
        return "", ""
    scheme, _, param = value.partition(" ")
    return scheme, param.strip()


def _auth_header(request):
    return {k.lower(): v for k, v in request.headers.items()}.get("authorization")


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


class HTTPBearer(SecurityBase):
    def __init__(self, *, bearerFormat=None, scheme_name=None,
                 description=None, auto_error=True):
        self.auto_error = auto_error
        self.scheme_name = scheme_name or self.__class__.__name__
        m = {"type": "http", "scheme": "bearer"}
        if bearerFormat is not None:
            m["bearerFormat"] = bearerFormat
        if description is not None:
            m["description"] = description
        self.model = m

    def _401(self):
        return HTTPException(401, "Not authenticated",
                             {"WWW-Authenticate": "Bearer"})

    async def __call__(self, request):
        auth = _auth_header(request)
        scheme, param = _scheme_param(auth)
        if not (auth and scheme and param):          # MUST-7 step 1
            if self.auto_error:
                raise self._401()
            return None
        if scheme.lower() != "bearer":               # MUST-7 step 2
            if self.auto_error:
                raise self._401()
            return None
        return HTTPAuthorizationCredentials(scheme=scheme, credentials=param)


class HTTPBasic(SecurityBase):
    def __init__(self, *, realm=None, scheme_name=None, description=None,
                 auto_error=True):
        self.realm = realm
        self.auto_error = auto_error
        self.scheme_name = scheme_name or self.__class__.__name__
        m = {"type": "http", "scheme": "basic"}
        if description is not None:
            m["description"] = description
        self.model = m

    def _hdrs(self):  # MUST-5
        return {"WWW-Authenticate": (f'Basic realm="{self.realm}"'
                                     if self.realm else "Basic")}

    async def __call__(self, request):
        auth = _auth_header(request)
        scheme, param = _scheme_param(auth)
        unauth = HTTPException(401, "Not authenticated", self._hdrs())
        if not auth or scheme.lower() != "basic":    # MUST-6 path 1 (auto_error-gated)
            if self.auto_error:
                raise unauth
            return None
        try:                                          # MUST-12: NOT auto_error-gated
            data = base64.b64decode(param).decode("ascii")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            raise unauth
        user, sep, pw = data.partition(":")
        if not sep:
            raise unauth
        return HTTPBasicCredentials(username=user, password=pw)


class OAuth2(SecurityBase):
    def __init__(self, *, flows=None, scheme_name=None, description=None,
                 auto_error=True):
        self.auto_error = auto_error
        self.scheme_name = scheme_name or self.__class__.__name__
        m = {"type": "oauth2", "flows": flows or {}}
        if description is not None:
            m["description"] = description
        self.model = m

    def _401(self):
        return HTTPException(401, "Not authenticated",
                             {"WWW-Authenticate": "Bearer"})

    async def __call__(self, request):
        auth = _auth_header(request)
        if not auth:                                  # MUST-9
            if self.auto_error:
                raise self._401()
            return None
        return auth                                   # raw full header


class OAuth2PasswordBearer(OAuth2):
    def __init__(self, tokenUrl, *, scopes=None, scheme_name=None,
                 description=None, auto_error=True, refreshUrl=None):
        flow = {"scopes": scopes or {}, "tokenUrl": tokenUrl}  # MUST-10 key order
        if refreshUrl is not None:
            flow = {"refreshUrl": refreshUrl, **flow}
        super().__init__(flows={"password": flow}, scheme_name=scheme_name,
                         description=description, auto_error=auto_error)

    async def __call__(self, request):
        auth = _auth_header(request)
        scheme, param = _scheme_param(auth)
        if not auth or scheme.lower() != "bearer":
            if self.auto_error:
                raise self._401()
            return None
        return param                                  # token only


class OAuth2PasswordRequestForm:
    """Form-dependency import surface. Full form parsing is M1-P6 (forms);
    the byte-exact 422 form shape is a P7 quarantine ledger item."""

    def __init__(self, *, grant_type=None, username="", password="",
                 scope="", client_id=None, client_secret=None):
        self.grant_type = grant_type
        self.username = username
        self.password = password
        self.scopes = scope.split()
        self.client_id = client_id
        self.client_secret = client_secret
