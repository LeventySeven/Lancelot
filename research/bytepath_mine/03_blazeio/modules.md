# Blazeio production modules — byte-path mechanism note

Source cluster: `server_utilities.py`, `safeguards.py`, `reasons.py`, `Other/proxy.py`,
`Other/asynctemplate.py`, plus `teardowns/BLAZEIO.md` (1132 lines, full read) and
`research/deep/06-blazeio-production-cloud-modules.md` (651 lines, full read).
All `Blazeio/` paths are relative to
`.../researchfms/blazeio/Blazeio/Blazeio/`.

Cross-checked against Lancelot M1–M19 surface (`lancelot/` Python + `src/` Rust)
and prior research (`research/blazeio/01–03`, `research/deep/06`).
Labels: **HAVE** = already in Lancelot; **NEW** = not yet present; **AVOID** = explicitly skip.

---

## 1. `WildcardRouteMatcher` — O(depth) longest-prefix fallback

**File:** `Modules/server_utilities.py:5-26`
**Mechanism:** `rfind("/")` loop over `r.path`, `max_del_count=20` cap, two
dict probes per iteration (`ref` and `ref + "/"`).  Called only on O(1) exact
miss — not on the hot path.  `urp=True` rebinds `r.path` to the matched
route's canonical name (normalizes the path for handler logic).

**Lancelot check:** Lancelot uses `matchit` (Rust radix trie via the PyO3
bridge, `src/router.rs`) for O(depth) parameterized routing — real path params
(`/api/{id}`) vs Blazeio's "parse the tail yourself" model.
`teardowns/BLAZEIO.md §5.2` notes the docstring's "O(1)" claim is overstated.

**Verdict:** **HAVE** (Lancelot's `matchit` routing is strictly better — typed
params, correct O(k) guarantee, Levenshtein 404 suggestions already shipped in
M7).

---

## 2. `SafeGuards.is_alive` — debug stub with print

**File:** `Modules/safeguards.py:6-16`
**Mechanism:** checks `r.__is_alive__` and raises `Err("Client has disconnected.")`.
Has a hardcoded `print(r.__is_alive__)` — not production-clean.  The real
disconnect reap happens in the `__ReMonitor__` watchdog thread
(`Dependencies/__init__.py:619-661`) by probing `transport.is_closing()` and
cancelling the request task.  `is_alive` itself is a dead letter in the hot path.

**Lancelot check:** Lancelot detects client disconnect via the ASGI `receive`
channel (`asgi.py:574` `_watch_disconnect`) and a `cancel_event` propagated into
`idle_guarded_chunks`/`relay` (`llm.py:36-67`).  The disconnect→cancel wiring is
in `asgi.py:1292-1301` and `asgi.py:1452-1479`.

**Verdict:** **HAVE** (Lancelot's model is cleaner: ASGI-native disconnect
detection, no debug print, full PEP-789 teardown ordering).

---

## 3. `StatusReason.reasons` — full RFC status-phrase map

**File:** `Modules/reasons.py:3-66`
**Mechanism:** a 57-entry class-level `dict`, `int→str`, covering 100–511
including 418 ("I'm a teapot").  Used in `prepare` (`server_protocol_extratools.py:307`)
to build `"HTTP/1.1 {code} {reason}\r\n"`.

**Lancelot check:** Lancelot has `lancelot/status.py`.

**Verdict:** **HAVE** — status codes already mapped.

---

## 4. Two-task relay topology — inherited end-to-end backpressure (PROXY CORE)

**File:** `Other/proxy.py:171-205` (`Transporter.transporter` + `Transporter.puller`)
**Mechanism:**
```
puller task (client→upstream):  while chunk := await r:  resp.push(chunk)
response direction (inline):    async for chunk in resp: r.writer(chunk)
teardown:                        task.cancel(); await task
```
Both ends are Blazeio's pull-model `BufferedProtocol` (one preallocated
`bytearray`, `pause_reading()` after each chunk).  Backpressure is **structural**:
slow client → `r.writer(chunk)` parks → inner `async for` stalls → upstream
TCP window closes → upstream stops sending.  No explicit buffer cap, no queue.

**What Blazeio is MISSING (the moat):** when the inbound client disconnects,
`puller` raises `ClientDisconnected` and stops, but the **response direction**
keeps draining the upstream until the next `r.writer(chunk)` fails.  No
`resp.abort()` is called proactively — tokens keep flowing (and billing) after
the client is gone.

**Lancelot check:** `lancelot/llm.py` `relay` + `_relay_forward` (lines 686–750)
implement the same topology via `pool.stream(..., request=request)` feeding
`idle_guarded_chunks`.  The disconnect→cancel wiring IS present
(`asgi.py:574` `_watch_disconnect` → `cancel_event` → `_race_chunk_vs_cancel`
in `llm.py:36-67`).

**Verdict:** **HAVE** (Lancelot ships the relay + the moat Blazeio omits).
The structural backpressure principle from `proxy.py` is the same end-to-end
pull model already in Lancelot's stream.rs bounded-mpsc channel.

---

## 5. TLS-record coalescer, SSE-exempt — `oldproxy.py`

**File:** `Other/proxy.py` predecessor — `oldproxy.py:131-161` (same tree, pass-1
deep read in `research/deep/06-blazeio-production-cloud-modules.md §1.2`)
**Mechanism:** buffer upstream chunks until `>= scope.tls_record_size` (256 B)
before writing downstream; **BUT** when `content-type == "text/event-stream"`,
`tls_record_size = 0` → every chunk flushes immediately.
```python
tls_record_size = scope.tls_record_size if resp.headers.get("content-type") != "text/event-stream" else 0
async for chunk in resp.pull():
    if not r.store.buff and len(chunk) >= tls_record_size:
        await r.write(chunk)   # big chunk, no pending: pass-through
        continue
    r.store.buff.extend(chunk)
    if len(r.store.buff) >= tls_record_size:
        _, __ = await r.write(r.store.buff), r.store.buff.clear()
```

**Rationale:** avoids the pathological "one byte per TLS record" overhead
(5-byte header + MAC + padding per record regardless of payload size).  The
SSE exemption is the critical detail: LLM tokens flush per-chunk; bulk bodies
coalesce.

**Lancelot check:** Lancelot's Rust streaming pump (`src/stream.rs`) uses a
bounded MPSC channel (depth `LANCELOT_STREAM_BUFFER`, default 256) plus
`hyper::body::StreamBody` — it does NOT apply the 256-byte TLS coalescer on the
relay path.  The `relay` in `llm.py` passes chunks straight through
`idle_guarded_chunks` with no minimum-write coalescing.

**NEW finding:** Lancelot is missing the content-type-gated coalescer for the
relay path.  For LLM relay (SSE), per-token flush is correct.  For non-SSE
relay (e.g. a `/v1/embeddings` JSON response), small upstream chunks create
small TLS records.  The fix: in the Rust service layer, detect `text/event-stream`
upstream content-type → flush per-chunk; else coalesce to >= 1 MTU (1460 B or
~256 B minimum) using the Rust write buffer before calling `hyper`'s
`poll_flush`.

**Byte-path relevance:** direct relay throughput win for non-SSE paths; no
change to LLM token latency (SSE path is exempt).  A Rust-side coalescer avoids
the Python `bytearray` churn Blazeio's `r.store.buff` introduces.

---

## 6. Header request-splice zero-parse injection

**File:** `Other/proxy.py:300-312` (`Server.r_parser`)
**Mechanism:** buffer raw bytes until `\r\n\r\n`; then find the first `\r\n`
(end of request line) and splice two pseudo-headers (`ip_host:`, `ip_port:`)
as raw bytes between the request line and the rest of the header block.
Result: the upstream receives the verbatim original head + injected lines —
zero header dict parse/re-serialize, O(1) splice, header order/casing preserved.

**Lancelot check:** Lancelot does not ship a transparent proxy (HTTP pass-through
is not in scope per cardinal rule 1 — Lancelot builds typed AI routes, not a
generic gateway).  For `X-Request-ID` / `traceparent` injection on outbound LLM
calls, Lancelot uses `pool.request()` which injects headers into a typed
`httpx.AsyncClient` request dict (`llm.py:765-799`) — the proxy-relay model does
not apply here.

**Verdict:** SKIP (not a byte-path win for Lancelot's AI/typed-API positioning;
the zero-parse splice is elegant but only relevant for a transparent proxy
Lancelot does not ship).

---

## 7. Host-keyed upstream + wildcard suffix routing

**File:** `Other/proxy.py:160-173` (`Server.__main_handler__`)
**Mechanism:** `app.hosts.get(server_hostname) or wildcard_srv(server_hostname)`;
`wildcard_srv` walks suffix (`"*.%s" % host_after_first_dot`, proxy.py:225-228).
`protocol_count` monotonic int → `r.identifier` registered in `app.protocols`
dict → single `protocol_manager` sweeper task watches all in-flight relays
(O(in-flight) memory, one timer, not N per-request timers).

**Lancelot check:** Lancelot routes AI calls via provider enum / URL in
`TransportPool.request()`, not an `httpx`-style host-keyed table.
Per-request timeout is Lancelot's per-route `timeout=` kwarg (dispatched in the
Rust `serve()` with a Tokio `sleep`).

**Verdict:** partial HAVE.  The ONE useful detail is the **single watchdog sweeper
over a registered-protocol slab** vs N per-request `asyncio.wait_for` timers.
Lancelot already uses Rust `timeout_by_route` (`dispatch.rs`), not a Python
sweeper — architecturally superior (no GIL).

---

## 8. `Batchedwriter` — application-layer write coalescing

**File:** `Modules/Server/writers.py:7-29`
**Mechanism:** async context-manager that monkeypatches `r.write` to buffer into
a `bytearray`; flushes in 10 KB units when buffer exceeds 30 KB; restores on
exit.  `min_chunk_size=1024*10`, `rem_chunk_size=*3` (30 KB threshold).
Socket runs `TCP_NODELAY=1` so this is a voluntary application-layer Nagle.

**Lancelot check:** Lancelot uses `TCP_NODELAY` on the Rust server socket
(`socket.rs:23`).  The Rust streaming pump's bounded MPSC channel + `hyper`
frame buffering plays the same coalescing role implicitly.  No explicit
`Batchedwriter` equivalent exists in Python.

**Verdict:** **NOT needed** (Lancelot's Rust layer buffers at the Tokio/hyper
level; adding a Python-layer `bytearray` coalescer above it would double-buffer
and add GIL hold time).

---

## 9. SNI / per-host TLS cert dispatch + hardened context

**File:** `Other/proxy.py:84-132` (`Sslproxy`)
**Mechanism:** `sni_callback` on `SSLContext` fires at `ClientHello` with the
SNI name; performs per-host cert lookup + lazy `ctx.load_cert_chain()` + cache.
Context hardening: `TLSVersion.TLSv1_3` minimum, `OP_NO_COMPRESSION` (CRIME
defense), ECDH curve `"prime256v1"`, `session_tickets=True` (RFC 5077).

**Lancelot check:** Lancelot is HTTP/1.1 cleartext behind a TLS-terminating
proxy (CLAUDE.md "HTTP/1.1-only (cleartext behind a TLS proxy)").  TLS is
terminated at the edge (K8s ingress / ALB).

**Verdict:** SKIP for Lancelot core.  The TLS context hardening defaults
(1.3-min, no-compression, P-256) are good patterns for outbound `TransportPool`
if it ever adds mTLS beyond the existing `reload_mtls()` surface — already noted
in `research/deep/06 §1.6`.

---

## 10. `TCPOptimizer` + proxy TCP socket option set

**File:** `Other/proxy.py:493-506` (`Runner.gen_web`) and
`Utils/socket_ops.py:19-33` (`TCPOptimizer`)
**Mechanism:** `TCP_NODELAY=1`, `SO_KEEPALIVE=1`, `TCP_KEEPIDLE=30`,
`TCP_KEEPINTVL=10`, `TCP_KEEPCNT=3`, `TCP_QUICKACK=1`, `TCP_FASTOPEN=5`
(all gracefully skipped on import error).  Keepalive triple: dead peer detected
in ~60 s (30 idle + 3×10 probes).

**Lancelot check:** Lancelot's `socket.rs` sets `TCP_NODELAY`, `SO_REUSEPORT`,
`TCP_QUICKACK` (Linux only), `TCP_FASTOPEN=5` (Linux only) — confirmed.
`SO_KEEPALIVE` + the three `KEEPIDLE/KEEPINTVL/KEEPCNT` probes are **NOT set**
on the Lancelot server socket.

**NEW finding:** TCP keepalive probes (`SO_KEEPALIVE` + `KEEPIDLE=30` +
`KEEPINTVL=10` + `KEEPCNT=3`) are missing from `socket.rs`.  Without them,
half-open TCP connections (client process died without FIN — common in AI
deploy scenarios: Lambda cold kill, k8s pod eviction, spot-instance termination)
are not reaped until a write fails.  The Rust `hyper` HTTP/1.1 server will hold
the connection and burn a slot in `max_connections` indefinitely.  Blazeio and
Robyn both set this triple.

**Byte-path relevance:** resilience — connection-cap headroom is the bottleneck
for concurrent AI workloads.  A half-open leak under `LANCELOT_MAX_CONNECTIONS`
blocks new requests.  Fix is 3 lines in `socket.rs` using `socket2`'s
`TcpKeepalive` builder (supported via `socket2::TcpKeepalive::new().with_time().with_interval().with_retries()`).

---

## 11. `Asynctemplate` — streaming template engine

**File:** `Other/asynctemplate.py:1-101`
**Mechanism:** chunk-at-a-time `<!--var-->` / `<!--var(instruction)-->` marker scan
using `chunk.find(b"<!--")` / `chunk.find(b"-->")`.  Supports encoder maps
(`strip_newlines`) and instruction maps (`stream_file`).  Yields before/content/after
as bytes.

**Known flaw (from teardown §8):** the marker scan is NOT resumable across chunk
boundaries — a `<!--` split across two chunks is missed.  Same non-resumable
parser anti-pattern as Blazeio's header accumulator.

**Lancelot check:** Lancelot has no templating surface (API-first, CLAUDE.md
"AI logic stays user-side").

**Verdict:** SKIP.  Noted only because the chunk-boundary-split is an
anti-pattern Lancelot must not replicate in any streaming marker scan (e.g.
the SSE `data:` frame scanner in `llm.py:75-116` uses `_SSE_SCAN_CAP` and
carries state across chunks — correctly resumable).

---

## 12. `X-Accel-Buffering: no` + `Cache-Control: no-cache` on streaming responses

**File:** `sqlite3io/server.py:63` (Blazeio's networked-SQLite server, cited in
`research/deep/06 §2.2`)
**Mechanism:** response headers `Cache-Control: no-store,no-cache,...`,
`X-Accel-Buffering: no` defeat nginx/CDN buffering of streaming responses.
`Content-type: video/mp4` is Blazeio's hack to prevent proxy transformation
(wrong; Lancelot uses `text/event-stream`).

**Lancelot check:** `asgi.py:1041-1042` emits
`(b"cache-control", b"no-cache")` and `(b"x-accel-buffering", b"no")` on every
`@app.stream` SSE response.

**Verdict:** **HAVE.**

---

## 13. `proxy.py` socket tuning: `SO_REUSEPORT` guard for non-Linux

**File:** `Other/proxy.py:13-26` (imports) and `Runner.gen_web:493`
**Mechanism:**
```python
try:
    from socket import SO_REUSEPORT
except ImportError:
    SO_REUSEPORT = None
...
if SO_REUSEPORT:
    web.sock().setsockopt(SOL_SOCKET, SO_REUSEPORT, 1)
```
Graceful skip on Windows/BSD where `SO_REUSEPORT` either doesn't exist or
doesn't distribute connections.

**Lancelot check:** `socket.rs` uses `#[cfg(unix)] sock.set_reuse_port(true)?`
— Unix-only guard, same discipline.

**Verdict:** **HAVE** (Lancelot already guards with `#[cfg(unix)]`).

---

## 14. `Dbstuff` hot-reload — file-size-delta poll (AVOID)

**File:** `Other/proxy.py:60-82`
**Mechanism:** background task polls `os.path.getsize()` every 30 s; reloads
`app.hosts` if size changed.  Webhook route (`/remote/webhook`) updates hosts
in-process and persists.  Change-detection is size-only (misses same-size edits;
30 s reconfiguration latency).

**Verdict:** AVOID (confirmed in `research/deep/06 §7b`).  Lancelot's hot-reload
pattern is SIGHUP + `lancelot.secrets.install_sighup_handler()` (already shipped,
M7).

---

## 15. Relay teardown correctness: `ConnectionTaskShield` as anti-marker

**File:** `Protocols/server_protocol.py:123-138`
**Mechanism:** sets `r.cancel_on_disconnect = False` for a critical section so
the watchdog doesn't cancel the handler on client disconnect.  Used by
`remote_update.py:21` so a git-pull finishes even if the HTTP client leaves.
This is the INVERSE of what a relay wants.

**Byte-path relevance:** marks the design boundary.  Lancelot's relay (in
`llm.py:686-750`) propagates disconnect → cancel → upstream.abort() — the
opposite of `ConnectionTaskShield`.  Confirmed that Blazeio's relay does NOT do
this (hence Lancelot's moat is real).

**Verdict:** HAVE (anti-pattern is already inverted in Lancelot).

---

## Summary table

| # | Mechanism | Lancelot status | Action |
|---|---|---|---|
| 1 | WildcardRouteMatcher | **HAVE** (matchit, better) | None |
| 2 | SafeGuards.is_alive | **HAVE** (ASGI disconnect + cancel_event) | None |
| 3 | StatusReason dict | **HAVE** (status.py) | None |
| 4 | Two-task relay + inherited backpressure | **HAVE** (relay + moat added) | None |
| 5 | TLS-record coalescer, SSE-exempt | **NEW** | Build in Rust relay writer |
| 6 | Raw-bytes proxy header splice | **SKIP** (transparent proxy not in scope) | None |
| 7 | Single watchdog sweeper slab | **HAVE** (Rust per-route timeout) | None |
| 8 | Batchedwriter app-layer Nagle | **NOT NEEDED** (Rust/hyper buffers) | None |
| 9 | SNI per-host TLS + context hardening | **SKIP** (TLS at edge) | Note for mTLS |
| 10 | TCP keepalive probe triple | **NEW** (socket.rs missing KEEPIDLE/INTVL/CNT) | Add to socket.rs |
| 11 | Asynctemplate | **SKIP** (out of scope) | Note chunk-split anti-pattern |
| 12 | X-Accel-Buffering: no on SSE | **HAVE** (asgi.py:1042) | None |
| 13 | SO_REUSEPORT non-Linux guard | **HAVE** (cfg(unix)) | None |
| 14 | File-size-delta hot-reload poll | **AVOID** | None |
| 15 | ConnectionTaskShield anti-marker | **HAVE** (correctly inverted) | None |

---

## Two NEW byte-path actions

### Action A — TCP keepalive probe triple (`socket.rs`)

Add to `bind_reuseport` in `socket.rs` after the existing `QUICKACK`/`FASTOPEN`
block:

```rust
// SO_KEEPALIVE + TCP_KEEPIDLE(30) + TCP_KEEPINTVL(10) + TCP_KEEPCNT(3):
// detect half-open connections (k8s pod eviction, Lambda hard-kill,
// spot-instance termination) in ~60 s (30 idle + 3×10 probes).
// Without this, dead-client slots are never reclaimed until the next
// failed write — burns LANCELOT_MAX_CONNECTIONS capacity.
// Blazeio proxy.py:503-506; Robyn server.py; both set this triple.
#[cfg(unix)]
{
    use socket2::TcpKeepalive;
    let ka = TcpKeepalive::new()
        .with_time(std::time::Duration::from_secs(30))
        .with_interval(std::time::Duration::from_secs(10));
    #[cfg(not(target_os = "macos"))]
    let ka = ka.with_retries(3);
    let _ = sock.set_tcp_keepalive(&ka);  // best-effort; fail silently
}
```

`socket2::TcpKeepalive::with_retries` is not available on macOS — gate with
`#[cfg(not(target_os = "macos"))]` (macOS default is 8 probes, acceptable).
`with_time` + `with_interval` compile on all Unix targets.

### Action B — Content-type-gated write coalescer for non-SSE relay

In the Rust relay/response path, when `content-type != text/event-stream`,
coalesce chunks to >= 1 MTU (1460 B) before calling `poll_flush`.  When
`content-type == text/event-stream`, flush per-chunk (current behavior).
This maps to `oldproxy.py:147` logic (`tls_record_size = 0` for SSE,
`>= 256` for bulk) but implemented Rust-side to avoid Python `bytearray`
churn.  Implementation point: `src/stream.rs` `_drive` already has the channel;
add a min-write-size parameter checked at the `send` site.

Honest scope: benefit is narrow — it only matters when:
  - Lancelot is NOT behind a TLS-terminating proxy (rare for production deploys)
  - AND the upstream returns many small chunks on a non-SSE path (e.g. bulk JSON
    from `/v1/embeddings`)

Lower priority than Action A.  Do not ship if it complicates the stream.rs
hot path for SSE.

---

*Source-level RE pass. Every citation verified against on-disk files.
RE date 2026-06-22.*
