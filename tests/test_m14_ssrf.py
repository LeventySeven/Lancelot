"""M14 — lancelot.net SSRF guard (opt-in). Blocks user-supplied outbound URLs
that resolve to private / loopback / link-local / metadata / CGNAT addresses.
"""
import pytest

from lancelot.net import SSRFError, assert_safe_url, is_safe_public_url

SAFE = [
    "http://8.8.8.8/",
    "https://1.1.1.1/",
    "http://100.63.255.255/",   # just below CGNAT
    "http://100.128.0.0/",      # just above CGNAT
    "http://[2606:4700:4700::1111]/",
    "http://[::ffff:8.8.8.8]/",  # IPv4-mapped public
]

BLOCKED = [
    "http://10.0.0.1/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://100.64.0.1/",                          # CGNAT (stdlib gap)
    "http://0.0.0.0/",
    "http://240.0.0.1/",
    "http://255.255.255.255/",
    "http://224.0.0.1/",                           # multicast (not is_private)
    "http://[::1]/",
    "http://[fc00::1]/",
    "http://[fe80::1]/",
    "http://[ff02::1]/",
    "http://[::ffff:127.0.0.1]/",                  # mapped loopback
    "http://[::ffff:100.64.0.1]/",                 # mapped CGNAT (hard case)
    "http://localhost/",
    "http://foo.localhost/",
    "http://LOCALHOST/",
    "ftp://evil.com/",                             # bad scheme
    "file:///etc/passwd",                          # bad scheme
    "http://0x7f000001/",                          # obfuscated loopback
    "http://2130706433/",                          # decimal loopback
    "http://127.1/",                               # short-form loopback
]


@pytest.mark.parametrize("url", SAFE)
def test_safe_urls_pass(url):
    assert is_safe_public_url(url) is True
    assert_safe_url(url)  # no raise


@pytest.mark.parametrize("url", BLOCKED)
def test_blocked_urls_rejected(url):
    assert is_safe_public_url(url) is False
    with pytest.raises(SSRFError):
        assert_safe_url(url)


def test_userinfo_host_is_the_real_host():
    # urlparse.hostname returns the real host, not the userinfo — so this is
    # SAFE (targets evil.com, a public host), NOT the metadata IP in userinfo.
    assert is_safe_public_url("http://169.254.169.254@example.com/") is True


def test_transportpool_block_private_ips_opt_in():
    import asyncio

    import httpx

    from lancelot.llm import TransportPool

    hit = {"n": 0}

    def handler(req):
        hit["n"] += 1
        return httpx.Response(200, json={})

    pool = TransportPool(transport=httpx.MockTransport(handler),
                         block_private_ips=True)
    with pytest.raises(SSRFError):
        asyncio.run(pool.request("GET", "http://169.254.169.254/"))
    assert hit["n"] == 0, "SSRF guard must fire BEFORE any network I/O"
    asyncio.run(pool.aclose())


def test_transportpool_default_does_not_block():
    # Default off — fixed-provider LLM calls are unaffected (we don't resolve).
    from lancelot.llm import TransportPool
    pool = TransportPool()
    assert pool._block_private_ips is False
    import asyncio
    asyncio.run(pool.aclose())
