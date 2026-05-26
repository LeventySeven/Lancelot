"""Opt-in SSRF guard. When a handler passes a USER-supplied URL to
lancelot.llm.TransportPool (webhook delivery, "fetch this url" tools), an
attacker could aim it at internal services or the cloud metadata endpoint
(169.254.169.254). `assert_safe_url` blocks any URL that resolves to a
non-public address.

STDLIB ONLY (ipaddress / socket / urllib.parse / time) — never pulls a runtime
dep; `import lancelot` does not import this module (TransportPool lazy-imports
it only when block_private_ips=True).

Defense notes:
- We always resolve via socket.getaddrinfo and check EVERY resolved IP — this
  also normalises obfuscated literals (0x7f000001, 2130706433, 127.1 → 127/8)
  that ipaddress.ip_address() would reject.
- IPv4-mapped IPv6 (::ffff:a.b.c.d) is unwrapped and re-checked.
- DNS-rebinding is a TOCTOU gap (we validate; httpx connects later). A 30 s DNS
  cache narrows it; a fully rebinding-proof fix needs a pinned-IP transport,
  out of scope — adequate for the webhook/tool-fetch threat model.
"""
from __future__ import annotations

import ipaddress
import os as _os
import socket
import time
from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})
# CGNAT (RFC 6598) is the ONE range no ipaddress flag covers — used by ISP
# carrier-grade NAT and many cloud/k8s overlay networks. Explicit check.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

try:
    DNS_CACHE_TTL_SECS: float = float(
        _os.environ.get("LANCELOT_SSRF_DNS_TTL_SECS", "30"))
except ValueError:
    DNS_CACHE_TTL_SECS = 30.0

_dns_cache: dict[tuple[str, int | None], tuple[float, list[str]]] = {}


class SSRFError(ValueError):
    """A URL targets a non-public / non-routable address. Subclasses
    ValueError so it surfaces as bad-input through validation layers."""


def _is_blocked_ip(addr_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(addr_str)
    except ValueError:
        return True  # unparseable resolved IP → treat as suspicious
    # IPv4-mapped IPv6 (::ffff:a.b.c.d): unwrap + re-check the embedded v4.
    # Critical for ::ffff:100.64.0.1 (CGNAT) which is_private misses.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return _is_blocked_ip(str(addr.ipv4_mapped))
    if (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
        return True
    # The only stdlib-flag gap: CGNAT.
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT:
        return True
    return False


def _resolve_all(hostname: str, port: int | None) -> list[str]:
    key = (hostname.lower(), port)
    now = time.monotonic()
    cached = _dns_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFError(f"DNS resolution failed for {hostname!r}: {exc}") from exc
    ips = list({r[4][0] for r in results})
    _dns_cache[key] = (now + DNS_CACHE_TTL_SECS, ips)
    return ips


def assert_safe_url(url: str) -> None:
    """Raise SSRFError unless `url` is a safe public http(s) destination."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(
            f"disallowed scheme {scheme!r}; only http/https allowed")
    hostname = parsed.hostname  # strips userinfo + IPv6 brackets
    if not hostname:
        raise SSRFError(f"URL has no resolvable hostname: {url!r}")
    lh = hostname.lower()
    if lh == "localhost" or lh.endswith(".localhost"):
        raise SSRFError(f"blocked hostname {hostname!r} (localhost)")
    for ip_str in _resolve_all(hostname, parsed.port):
        if _is_blocked_ip(ip_str):
            raise SSRFError(
                f"URL {url!r} resolves to a non-public address ({ip_str!r})")


def is_safe_public_url(url: str) -> bool:
    try:
        assert_safe_url(url)
        return True
    except SSRFError:
        return False
