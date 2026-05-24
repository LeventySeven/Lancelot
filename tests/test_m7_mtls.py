"""M7 T3.4 — mTLS toggle on TransportPool (Infisical PKI pattern).

Verifies argument parsing + httpx client construction + reload_mtls()
re-opens the client. NO real TLS handshake in tests (would need a
self-signed CA + a real server) — that's covered by an integration
smoke when an operator wires this up. We generate REAL self-signed
cert/key PEM files via `cryptography` so httpx's eager cert-load
doesn't raise.
"""
import asyncio
import datetime
import os
import tempfile

import httpx
import pytest

pytest.importorskip("httpx")
pytest.importorskip("cryptography")

from lancelot.llm import TransportPool


def _make_self_signed(dir_):
    """Generate a self-signed cert + private key (PEM) inside `dir_`.
    Returns (cert_path, key_path). Used as valid mTLS material that
    httpx loads without error."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lancelot-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subj)
        .issuer_name(subj)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = os.path.join(dir_, "cert.pem")
    key_path = os.path.join(dir_, "key.pem")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))
    return cert_path, key_path


# ----- T3.4.a cert + key together ----------------------------------------

def test_t3_4_mtls_cert_and_key_paired():
    """Passing only one of cert/key raises ValueError."""
    with pytest.raises(ValueError, match="must be set together"):
        TransportPool(mtls_cert="/tmp/cert.pem")
    with pytest.raises(ValueError, match="must be set together"):
        TransportPool(mtls_key="/tmp/key.pem")


def test_t3_4_mtls_both_set_succeeds():
    """Both cert and key set with valid PEM material → no error."""
    with tempfile.TemporaryDirectory() as d:
        cert, key = _make_self_signed(d)
        pool = TransportPool(mtls_cert=cert, mtls_key=key)
        assert pool._mtls_cert == cert
        assert pool._mtls_key == key

        async def cleanup():
            await pool.aclose()

        asyncio.run(cleanup())


# ----- T3.4.b ca toggle --------------------------------------------------

def test_t3_4_mtls_ca_toggle():
    """Custom CA bundle is passed through as httpx `verify=`."""
    with tempfile.TemporaryDirectory() as d:
        cert, key = _make_self_signed(d)
        # Use the same cert as a stand-in CA (httpx just verifies it
        # parses as PEM at construction time).
        ca = cert
        pool = TransportPool(mtls_cert=cert, mtls_key=key, mtls_ca=ca)
        assert pool._mtls_ca == ca

        async def cleanup():
            await pool.aclose()
        asyncio.run(cleanup())


# ----- T3.4.c default off (no mTLS) --------------------------------------

def test_t3_4_mtls_default_off():
    pool = TransportPool()
    assert pool._mtls_cert is None
    assert pool._mtls_key is None
    assert pool._mtls_ca is None

    async def cleanup():
        await pool.aclose()
    asyncio.run(cleanup())


# ----- T3.4.d reload_mtls re-opens the client (smoke) --------------------

def test_t3_4_reload_mtls_swaps_client():
    """reload_mtls() replaces the underlying httpx client. The old
    one is closed; in-flight calls would finish on the old client."""
    with tempfile.TemporaryDirectory() as d:
        cert, key = _make_self_signed(d)
        pool = TransportPool(mtls_cert=cert, mtls_key=key)

        async def run():
            client_before = pool._client
            await pool.reload_mtls()
            client_after = pool._client
            assert client_after is not client_before
            await pool.aclose()

        asyncio.run(run())


def test_t3_4_reload_mtls_no_mtls_is_noop():
    """reload_mtls() on a pool that wasn't configured with mTLS is a
    no-op (no new client, no error)."""
    pool = TransportPool()

    async def run():
        c0 = pool._client
        await pool.reload_mtls()
        assert pool._client is c0  # unchanged
        await pool.aclose()

    asyncio.run(run())
