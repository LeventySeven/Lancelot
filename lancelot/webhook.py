"""Webhook signing/verification — Standard Webhooks scheme (standardwebhooks.com,
the same scheme Render/Svix use). STDLIB-ONLY (hmac/hashlib/base64/time/uuid) so
it never touches the pydantic-only runtime rule.

This is the crypto + header construction ONLY — NOT a delivery engine, NOT
retries (those are user-side / the proxy's job). Lancelot owns the byte path:
when your handler posts a result to a user-supplied URL, sign it so the
receiver can verify it came from you; when you receive a webhook, verify it.

    from lancelot.webhook import sign, verify
    headers = sign(body_bytes, secret)                 # attach to outbound POST
    ok = verify(body_bytes, request.headers, secret)   # inbound check → 401/403

Secret: a base64 key, optionally `whsec_`-prefixed (Standard Webhooks). Generate
one with `"whsec_" + base64.b64encode(os.urandom(32)).decode()`.

Replay protection: `verify` enforces a timestamp freshness window, but dedup of
a replayed `webhook-id` is the caller's job (store seen ids with a TTL ≥
tolerance_s). Firecrawl's simpler `sha256=HMAC(secret, body)` scheme has no
timestamp/replay protection; this is the safer default.
"""
import base64
import hmac
import time
import uuid as _uuid
from collections.abc import Mapping

_WHSEC = "whsec_"
_MIN_KEY = 24
_MAX_KEY = 64


def _key_bytes(secret: str) -> bytes:
    # `whsec_`-prefixed or bare base64; `+ "=="` absorbs missing padding.
    raw = secret[len(_WHSEC):] if secret.startswith(_WHSEC) else secret
    return base64.b64decode(raw + "==")


def sign(payload: bytes, secret: str, *,
         msg_id: str | None = None,
         timestamp: int | None = None) -> dict[str, str]:
    """Return the Standard-Webhooks headers for an outbound request. `payload`
    is the EXACT bytes you send (don't re-serialize). `msg_id` is stable across
    retries (default uuid4); it must not contain '.' (the field separator)."""
    if msg_id is None:
        msg_id = str(_uuid.uuid4())
    if "." in msg_id:
        raise ValueError(f"webhook msg_id must not contain '.': {msg_id!r}")
    ts = int(time.time()) if timestamp is None else int(timestamp)
    key = _key_bytes(secret)
    if not (_MIN_KEY <= len(key) <= _MAX_KEY):
        raise ValueError(
            f"webhook secret must decode to {_MIN_KEY}-{_MAX_KEY} bytes, "
            f"got {len(key)} (generate with "
            "'whsec_' + base64.b64encode(os.urandom(32)).decode())")
    signed = f"{msg_id}.{ts}.".encode("utf-8") + payload
    sig = base64.b64encode(hmac.new(key, signed, "sha256").digest()).decode()
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": f"v1,{sig}",
    }


def verify(payload: bytes, headers: Mapping, secret: str, *,
           tolerance_s: int = 300) -> bool:
    """Verify an inbound Standard-Webhooks request. Returns True/False (never
    raises). Constant-time signature compare; timestamp freshness window
    (tolerance_s, 0 = disable); accepts any of multiple space-separated `v1,`
    signatures (key rotation). Replay dedup on `webhook-id` is the caller's."""
    h = {k.lower(): v for k, v in headers.items()}
    msg_id = h.get("webhook-id")
    ts_str = h.get("webhook-timestamp")
    sig_hdr = h.get("webhook-signature")
    if not (msg_id and ts_str and sig_hdr):
        return False
    try:
        ts = int(ts_str)
    except (ValueError, TypeError):
        return False
    if tolerance_s > 0 and abs(int(time.time()) - ts) > tolerance_s:
        return False
    try:
        key = _key_bytes(secret)
        if not (_MIN_KEY <= len(key) <= _MAX_KEY):
            return False  # misconfigured short/long key → fail closed, same as sign()
    except Exception:  # noqa: BLE001 — malformed secret → fail closed
        return False
    signed = f"{msg_id}.{ts_str}.".encode("utf-8") + payload
    expected = hmac.new(key, signed, "sha256").digest()
    for entry in sig_hdr.split():
        if not entry.startswith("v1,"):
            continue  # skip unknown/asymmetric versions
        try:
            candidate = base64.b64decode(entry[3:] + "==")
        except Exception:  # noqa: BLE001
            continue
        if hmac.compare_digest(expected, candidate):
            return True
    return False
