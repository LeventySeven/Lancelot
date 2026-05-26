"""M14 — lancelot.webhook: Standard Webhooks HMAC sign/verify helpers
(stdlib only). Sign/verify primitives, NOT a delivery engine.
"""
import base64
import hmac
import time

import pytest

from lancelot.webhook import sign, verify

SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo96sAzT3Bm3Z1FmJRA="


def test_sign_returns_three_headers():
    h = sign(b'{"x":1}', SECRET)
    assert set(h) == {"webhook-id", "webhook-timestamp", "webhook-signature"}


def test_sign_signature_prefix_v1():
    h = sign(b"body", SECRET)
    assert h["webhook-signature"].startswith("v1,")


def test_sign_matches_independent_hmac():
    """sign() must HMAC exactly `{id}.{ts}.{body}` with the base64-decoded key."""
    h = sign(b'{"a":1}', SECRET, msg_id="msg_1", timestamp=1674087231)
    key = base64.b64decode("MfKQ9r8GKYqrTwjUPD8ILPZIo96sAzT3Bm3Z1FmJRA==")
    signed = b"msg_1.1674087231." + b'{"a":1}'
    expect = "v1," + base64.b64encode(
        hmac.new(key, signed, "sha256").digest()).decode()
    assert h["webhook-signature"] == expect


def test_verify_roundtrip():
    body = b'{"event":"ping"}'
    assert verify(body, sign(body, SECRET), SECRET) is True


def test_verify_wrong_secret():
    body = b"body"
    other = "whsec_" + base64.b64encode(b"\x11" * 32).decode()
    assert verify(body, sign(body, SECRET), other) is False


def test_verify_tampered_payload():
    h = sign(b"original", SECRET)
    assert verify(b"tampered", h, SECRET) is False


def test_verify_stale_timestamp():
    h = sign(b"b", SECRET, timestamp=int(time.time()) - 301)
    assert verify(b"b", h, SECRET, tolerance_s=300) is False


def test_verify_fresh_at_boundary():
    h = sign(b"b", SECRET, timestamp=int(time.time()) - 299)
    assert verify(b"b", h, SECRET, tolerance_s=300) is True


def test_verify_future_outside_tolerance():
    h = sign(b"b", SECRET, timestamp=int(time.time()) + 301)
    assert verify(b"b", h, SECRET, tolerance_s=300) is False


def test_verify_tolerance_zero_disables_freshness():
    h = sign(b"b", SECRET, timestamp=1)  # ancient
    assert verify(b"b", h, SECRET, tolerance_s=0) is True


def test_verify_missing_header():
    h = sign(b"b", SECRET)
    del h["webhook-id"]
    assert verify(b"b", h, SECRET) is False


def test_verify_multisig_either_matches():
    body = b"b"
    base = sign(body, SECRET)                  # one msg_id + timestamp
    good = base["webhook-signature"]           # its real signature
    wrong = "v1," + base64.b64encode(b"\x00" * 32).decode()
    hdrs = dict(base)
    # good first
    hdrs["webhook-signature"] = good + " " + wrong
    assert verify(body, hdrs, SECRET) is True
    # good second
    hdrs["webhook-signature"] = wrong + " " + good
    assert verify(body, hdrs, SECRET) is True
    # neither
    hdrs["webhook-signature"] = wrong + " " + wrong
    assert verify(body, hdrs, SECRET) is False


def test_sign_msg_id_with_dot_rejected():
    with pytest.raises(ValueError):
        sign(b"b", SECRET, msg_id="contains.dot")


def test_whsec_prefix_and_bare_equivalent():
    bare = "MfKQ9r8GKYqrTwjUPD8ILPZIo96sAzT3Bm3Z1FmJRA="
    body = b"b"
    assert verify(body, sign(body, SECRET), bare) is True


def test_empty_body_signs_and_verifies():
    assert verify(b"", sign(b"", SECRET), SECRET) is True


def test_headers_case_insensitive():
    h = sign(b"b", SECRET)
    upper = {k.upper(): v for k, v in h.items()}
    assert verify(b"b", upper, SECRET) is True


def test_verify_rejects_empty_key_forged_signature():
    """Regression: verify() must fail-closed for a 0-byte (empty) key.

    Before the fix, verify() omitted the key-length check that sign() enforces.
    An attacker who knows the server is misconfigured with secret='whsec_' (which
    decodes to 0 bytes) can compute a valid HMAC and forge ANY payload.
    After the fix, verify() with an empty or short key returns False regardless
    of whether the HMAC is mathematically correct.
    """
    empty_key_secret = "whsec_"  # base64.b64decode("" + "==") == b""
    payload = b'{"event":"order","amount":9999}'
    msg_id = "msg_forged_001"
    ts_str = "1700000000"

    # Attacker computes a valid HMAC under the empty key.
    key = b""
    signed_bytes = f"{msg_id}.{ts_str}.".encode("utf-8") + payload
    forged_sig = "v1," + base64.b64encode(
        hmac.new(key, signed_bytes, "sha256").digest()
    ).decode()
    headers = {
        "webhook-id": msg_id,
        "webhook-timestamp": ts_str,
        "webhook-signature": forged_sig,
    }

    # Must be rejected even though the HMAC arithmetic is correct.
    assert verify(payload, headers, empty_key_secret, tolerance_s=0) is False
