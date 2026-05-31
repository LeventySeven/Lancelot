"""M17 P5 — constant-time static-token / API-key gate (`StaticTokenGate`).

An OPT-IN, stdlib-only ASGI middleware that compares an inbound bearer/API-key
token against a fixed allow-list in CONSTANT TIME and emits a byte-exact
**401 + WWW-Authenticate** on miss — WITHOUT entering dependency injection.
This fills the gap that `security.py` leaves: those schemes only EXTRACT the
credential (`HTTPBearer`/`OAuth2PasswordBearer`/`APIKeyHeader` return the
value); nothing in the tree COMPARES an inbound token against a known set.

Wiring (typical)::

    app = lancelot.App()
    app.add_middleware(
        StaticTokenGate,
        tokens=["s3cr3t-..."],          # or read from lancelot.secrets
        scheme="Bearer",                # Authorization: Bearer <token>
        guard_prefixes=("/v1",),        # only these path prefixes are gated
        open_paths=("/livez", "/readyz"),
        skip_options=True,              # let CORS preflight through
    )

Default behavior is byte-identical when the middleware is NOT added.

PANEL FENCE (binding): this is a STATIC-TOKEN gate, NOT a JWT/OAuth framework.
It does NOT decode JWTs, validate signatures/expiry, or enforce scopes — that
is user-side / `lancelot.security`. It only answers "is the presented token one
of these N known strings?" in constant time, which is exactly the cheap
machine-to-machine / internal-API guard vLLM and Prefect hand-roll (and diverge
on the Host-spoof + allowlist subtleties this module gets right).

Security properties:
  * **Constant-time compare** — every configured token is hashed once at
    construction with SHA-256; the presented token is hashed the same way and
    compared via ``hmac.compare_digest`` against each digest. Comparing
    fixed-width digests (not the raw strings) means neither the token VALUE nor
    its LENGTH leaks through compare timing, and the OR-loop runs over ALL
    tokens (no early return) so match POSITION doesn't leak either.
  * **Host-spoof safe** — the guard decision reads ``scope['path']`` only,
    never ``request.url`` / the Host header (a forged Host must not move a
    guarded path out of the guarded set).
"""
import hashlib
import hmac
from typing import Iterable, Sequence

__all__ = ["StaticTokenGate"]


def _digest(token: str) -> bytes:
    # Fixed 32-byte digest regardless of token length → compare timing reveals
    # neither value nor length.
    return hashlib.sha256(token.encode("utf-8")).digest()


class StaticTokenGate:
    """Pure-ASGI constant-time static-token gate (opt-in).

    Args:
        app: the inner ASGI app (set by ``add_middleware``).
        tokens: the allow-list of valid token strings (hashed at construction;
            the plaintext is not retained). Empty → every guarded request is
            rejected (fail-closed).
        scheme: the Authorization scheme to expect, e.g. ``'Bearer'``. The
            presented header must be ``"<scheme> <token>"`` (scheme matched
            case-insensitively). Use ``scheme=''`` to treat the whole
            Authorization header value as the token (raw API key).
        guard_prefixes: only request paths starting with one of these prefixes
            are gated; everything else passes untouched. Default ``('/',)``
            (gate everything).
        open_paths: exact paths that always pass ungated even under a guarded
            prefix (health probes). Default ``('/livez', '/readyz')``.
        skip_options: when True (default), ``OPTIONS`` requests pass ungated so
            CORS preflight is not blocked.
        header: the header to read the credential from. Default
            ``'authorization'``; set e.g. ``'x-api-key'`` for an API-key gate
            (with ``scheme=''``).
    """

    def __init__(self, app, *,
                 tokens: Iterable[str] = (),
                 scheme: str = "Bearer",
                 guard_prefixes: Sequence[str] = ("/",),
                 open_paths: Sequence[str] = ("/livez", "/readyz"),
                 skip_options: bool = True,
                 header: str = "authorization"):
        self.app = app
        self._digests = [_digest(t) for t in tokens]
        self._scheme = scheme or ""
        self._scheme_lc = self._scheme.lower()
        # Normalize prefixes/paths to plain str tuples (path comparison is on
        # scope['path'], which is a str).
        self._guard_prefixes = tuple(guard_prefixes)
        self._open_paths = frozenset(open_paths)
        self._skip_options = skip_options
        self._header_b = header.lower().encode("ascii")
        # Precompute the WWW-Authenticate value (RFC 7235): the scheme name, or
        # 'Bearer' as a sensible default when scheme='' (raw API key).
        challenge = self._scheme if self._scheme else "Bearer"
        self._www_auth = challenge.encode("latin1")

    def _is_guarded(self, path: str) -> bool:
        if path in self._open_paths:
            return False
        return path.startswith(self._guard_prefixes)

    def _token_ok(self, presented: str) -> bool:
        # Constant-time: hash the presented token once, compare against every
        # configured digest WITHOUT early return so neither value, length, nor
        # match position leaks via timing. (compare_digest is itself
        # constant-time over the 32-byte digests.)
        pd = _digest(presented)
        ok = False
        for d in self._digests:
            if hmac.compare_digest(pd, d):
                ok = True
        return ok

    def _extract(self, scope) -> str:
        """Pull the presented token from the configured header. Returns ''
        when absent/malformed (→ treated as a miss)."""
        raw = None
        for k, v in scope.get("headers", ()):
            if k == self._header_b:
                raw = v
                break
        if raw is None:
            return ""
        try:
            val = raw.decode("latin1").strip()
        except Exception:  # noqa: BLE001
            return ""
        if not val:
            return ""
        if not self._scheme:
            return val                      # raw API key: whole header value
        scheme, _, param = val.partition(" ")
        if scheme.lower() != self._scheme_lc:
            return ""                       # wrong scheme → miss
        return param.strip()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self._skip_options and scope.get("method") == "OPTIONS":
            return await self.app(scope, receive, send)
        # ALWAYS use scope['path'] — never request.url / Host (anti Host-spoof).
        path = scope.get("path", "/")
        if not self._is_guarded(path):
            return await self.app(scope, receive, send)
        presented = self._extract(scope)
        if presented and self._token_ok(presented):
            return await self.app(scope, receive, send)
        # Reject — byte-exact 401 + WWW-Authenticate, no DI entered.
        body = b'{"detail":"Not authenticated"}'
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"www-authenticate", self._www_auth),
        ]
        await send({"type": "http.response.start", "status": 401,
                    "headers": headers})
        await send({"type": "http.response.body", "body": body})
