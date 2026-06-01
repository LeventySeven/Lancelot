//! The hyper accept loop + the `run()` entrypoint — closes the M0 vertical
//! slice (socket → hyper → matchit → LazyRequest → GIL choreography →
//! pydantic-core → 200/404).
//!
//! Deviation from the plan's literal Task 10 code (necessary, same root cause
//! as Task 8 — see review): the plan's `serve()` did a bare `rt.block_on(accept
//! loop)` and called `call_async(handler, body)` (2-arg, ambient
//! `into_future`). In pyo3-async-runtimes 0.27 the Python coroutines only make
//! progress while an asyncio loop is *running*. So the server owns a long-lived
//! `asyncio.new_event_loop()` + `TaskLocals`, and the hyper accept loop is the
//! driver future of `tokio::run_until_complete(event_loop, …)` — that single
//! call spins the asyncio loop AND the tokio runtime together. `call_async` is
//! the 3-arg `(TaskLocals, Py<PyAny>, Bytes)` form from Task 8. Handler errors
//! map to 500 (no `.expect`/panic across the FFI seam).
use crate::dispatch::{await_and_serialize, build_result, Dispatched, ReqParts};
use crate::router::Router;
use bytes::Bytes;
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full, Limited};
use hyper::{body::Incoming, Request as HReq, Response as HResp, StatusCode};
use hyper_util::rt::{TokioIo, TokioTimer};
use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;
use pyo3_async_runtimes::TaskLocals;
use std::sync::Arc;

/// Uniform response body: a one-shot Full (regular JSON) OR a chunked stream
/// (R3 async-gen). BoxBody erases the two into one type so `handle` has a
/// single return type for the hyper service_fn.
type RespBody = BoxBody<Bytes, std::io::Error>;

/// Max request body the production path will buffer before returning 413
/// (audit CRITICAL #1 — the M0 path collected unbounded bodies, a trivial
/// remote memory-DoS). Default 16 MiB; tunable via `LANCELOT_MAX_BODY`
/// (clamped [1 KiB, 1 GiB]) — mirrors the Python ASGI seam's `_resolve_max_body`.
/// Upload APIs raise it; tight JSON microservices lower it.
fn max_body() -> usize {
    static N: std::sync::OnceLock<usize> = std::sync::OnceLock::new();
    *N.get_or_init(|| {
        std::env::var("LANCELOT_MAX_BODY")
            .ok()
            .and_then(|s| s.trim().parse::<usize>().ok())
            .map(|n| n.clamp(1024, 1024 * 1024 * 1024))
            .unwrap_or(16 * 1024 * 1024)
    })
}

/// Max concurrent connections the accept loop will serve (domain-web skill:
/// an unbounded `loop { accept; spawn }` is a DoS-amplification vector — a
/// connection flood spawns unbounded tasks). A bounded Semaphore acquired
/// BEFORE accept gives kernel-backlog backpressure (the OS buffers, then
/// refuses SYNs) rather than buffering accepted-but-unserved connections in
/// memory. Permit is held for the connection's lifetime, released on close.
/// Default 1024; tunable via `LANCELOT_MAX_CONNECTIONS` (clamped [1, 1<<20]) —
/// a real operator knob (match it to the container's FD limit) AND what lets
/// the connection-cap test run with a small cap.
fn max_connections() -> usize {
    static N: std::sync::OnceLock<usize> = std::sync::OnceLock::new();
    *N.get_or_init(|| {
        std::env::var("LANCELOT_MAX_CONNECTIONS")
            .ok()
            .and_then(|s| s.trim().parse::<usize>().ok())
            .map(|n| n.clamp(1, 1 << 20))
            .unwrap_or(1024)
    })
}

/// Graceful-drain deadline (B1): on SIGTERM we stop accepting, then wait up to
/// this long for in-flight connections to finish before closing — drain, not
/// Robyn-style hard-kill. Default 10 s; tunable via `LANCELOT_DRAIN_SECS`
/// (clamped [0, 300]; 0 = no drain, exit immediately).
fn drain_timeout() -> std::time::Duration {
    static S: std::sync::OnceLock<u64> = std::sync::OnceLock::new();
    let secs = *S.get_or_init(|| {
        std::env::var("LANCELOT_DRAIN_SECS")
            .ok()
            .and_then(|s| s.trim().parse::<u64>().ok())
            .map(|n| n.min(300))
            .unwrap_or(10)
    });
    std::time::Duration::from_secs(secs)
}

/// Slow-loris header-read deadline (R6). How long a client may take to send the
/// complete request head before the connection is dropped. Default 15 s
/// (Granian/nginx-class); tunable via `LANCELOT_HEADER_TIMEOUT_MS` (clamped
/// [100 ms, 600 s]) — a real operator knob AND what lets the slow-loris test
/// run in ~1 s instead of 15.
fn header_timeout_ms() -> u64 {
    static MS: std::sync::OnceLock<u64> = std::sync::OnceLock::new();
    *MS.get_or_init(|| {
        std::env::var("LANCELOT_HEADER_TIMEOUT_MS")
            .ok()
            .and_then(|s| s.trim().parse::<u64>().ok())
            .map(|n| n.clamp(100, 600_000))
            .unwrap_or(15_000)
    })
}

/// A prebaked response (Robyn-style ConstRouter / const-bytes tier): a route
/// whose reply is FIXED bytes, served straight from Rust with ZERO GIL, ZERO
/// `Request` allocation, ZERO handler call. The status + content-type are
/// validated at registration so `response()` cannot panic on the hot path.
/// Use for health probes (`/livez`/`/readyz`) and static/cached responses —
/// the small-response regime where the FFI+asyncio-bridge overhead otherwise
/// loses to uvicorn+uvloop.
pub struct Prebaked {
    status: StatusCode,
    content_type: hyper::header::HeaderValue,
    body: Bytes,
}

impl Prebaked {
    pub fn new(status: u16, content_type: &str, body: Vec<u8>) -> Result<Self, String> {
        Ok(Self {
            status: StatusCode::from_u16(status).map_err(|e| e.to_string())?,
            content_type: hyper::header::HeaderValue::from_str(content_type)
                .map_err(|e| format!("bad const content-type: {e}"))?,
            body: Bytes::from(body),
        })
    }

    /// Build the response — `body.clone()` is a cheap refcount bump (Bytes).
    /// `.unwrap()` is infallible: status + content_type were validated in `new`.
    fn response(&self) -> HResp<RespBody> {
        HResp::builder()
            .status(self.status)
            .header(hyper::header::CONTENT_TYPE, self.content_type.clone())
            .body(Full::new(self.body.clone()).map_err(|e| match e {}).boxed())
            .unwrap()
    }
}

pub struct App {
    pub router: Router,
    pub handlers: Vec<Py<PyAny>>, // slot -> handler
    // slot -> Some(prebaked) for const routes, None for handler routes. Parallel
    // to `handlers`. A const slot is served from Rust without ever entering
    // Python (the const-bytes fast path in `handle`).
    pub const_bodies: Vec<Option<Prebaked>>,
    pub locals: TaskLocals, // the server's asyncio loop + context
    // Notified on graceful shutdown (SIGTERM) so in-flight R3 stream pumps
    // stop pulling before the event loop closes (audit #2).
    pub shutdown: std::sync::Arc<tokio::sync::Notify>,
    // M18 Task#7: zero-arg Python callables returning a coroutine that drives
    // the ASGI lifespan protocol (built in `app.py::_make_lifespan_callbacks`,
    // reusing the existing `asgi._lifespan` machinery). `on_startup` runs on
    // the server's OWN loop BEFORE binding (a raise -> refuse to serve);
    // `on_shutdown` runs AFTER the connection drain, bounded by drain_timeout().
    // None -> lifespan-less app (zero cost; the hot path is untouched either way
    // — these are bind-time + drain-time only). NOT touched by `handle()`.
    pub on_startup: Option<Py<PyAny>>,
    pub on_shutdown: Option<Py<PyAny>>,
}

fn plain(status: StatusCode, body: &'static [u8]) -> HResp<RespBody> {
    HResp::builder()
        .status(status)
        .body(
            Full::new(Bytes::from_static(body))
                .map_err(|e| match e {})
                .boxed(),
        )
        .unwrap()
}

/// M7 T4.10: owned-bytes plain response for dynamically-built bodies
/// (e.g. the "did you mean?" 404 suggestion). Same shape as `plain`,
/// just `Bytes::from(Vec)` instead of `from_static`.
fn plain_owned(status: StatusCode, body: Vec<u8>) -> HResp<RespBody> {
    HResp::builder()
        .status(status)
        .body(Full::new(Bytes::from(body)).map_err(|e| match e {}).boxed())
        .unwrap()
}

fn json_ok(body: Bytes) -> HResp<RespBody> {
    HResp::builder()
        .status(StatusCode::OK)
        .header("content-type", "application/json")
        // `body` is already `Bytes` (pydantic-core output copied once in
        // serialize.rs) — no second Vec→Bytes alloc here.
        .body(Full::new(body).map_err(|e| match e {}).boxed())
        .unwrap()
}

async fn handle(app: Arc<App>, req: HReq<Incoming>) -> HResp<RespBody> {
    // Route on BORROWED method/path FIRST — a 404/405 then allocates NOTHING
    // (perf F3: the eager owned-String + per-header Vec build is deferred past
    // the route decision, restoring the lazy-Request "pay nothing if unmatched"
    // property; scanning / probe traffic no longer pays the allocation tax).
    let slot = match app.router.route(req.method().as_str(), req.uri().path()) {
        Some(s) => s,
        // 405 if the path exists under another method; 404 otherwise
        // (HTTP semantics / FastAPI parity — domain-web).
        None if app.router.matches_any_method(req.uri().path()) => {
            return plain(StatusCode::METHOD_NOT_ALLOWED, b"method not allowed")
        }
        // M7 T4.10: suggest the closest registered route on 404 (LanceDB
        // "did you mean?" pattern). Pays nothing on the happy path; fires
        // only when no route matches. Body is plain text; bounded length.
        None => {
            let path = req.uri().path();
            let body = match app.router.closest_path(path) {
                Some(suggestion) => {
                    format!("not found. did you mean '{suggestion}'?").into_bytes()
                }
                None => b"not found".to_vec(),
            };
            return plain_owned(StatusCode::NOT_FOUND, body);
        }
    };
    // Const-bytes fast path (Robyn ConstRouter): if this slot has a prebaked
    // response, serve it straight from Rust — ZERO GIL, ZERO Request alloc,
    // ZERO handler call. A Vec index + match, so it's ~free when no route is
    // const (all entries None). This is the path that beats uvicorn+uvloop on
    // tiny responses (health probes, static/cached endpoints).
    if let Some(Some(pre)) = app.const_bodies.get(slot) {
        return pre.response();
    }
    // Audit #5: bounds-checked slot lookup (router/handlers desync would
    // otherwise panic OUTSIDE catch_unwind). Should be unreachable given the
    // 1:1 build in lib.rs, but enforce rather than assume.
    let handler = match app.handlers.get(slot) {
        Some(h) => crate::gil::with_gil(|py| h.clone_ref(py)),
        None => return plain(StatusCode::INTERNAL_SERVER_ERROR, b"internal error"),
    };
    // Matched: NOW materialize the owned request parts (R2 — the REAL method /
    // path / query / headers, owned so `ReqParts` is `Send` across the `.await`).
    let method = req.method().as_str().to_string();
    let path = req.uri().path().to_string();
    let query_string = req.uri().query().unwrap_or("").to_string();
    // M7 T4.14 audit (RIPGREP.md:247 + 494 scratch-pool pattern): we don't
    // pool the headers vec because (a) per-request alloc cost is ~100ns and
    // dwarfed by the GIL+handler call (~50µs+), (b) `bytes::BytesMut` /
    // `String` already use jemalloc small-bins so pool gains are minimal.
    // We DO pre-size the vec from the HeaderMap len() so collect() doesn't
    // realloc mid-build — the only no-regret micro-win at this site.
    let hm = req.headers();
    let mut headers: Vec<(String, String)> = Vec::with_capacity(hm.len());
    for (k, v) in hm.iter() {
        // Starlette decodes header values as latin-1 (1 byte → 1 char),
        // which is lossless for arbitrary bytes — unlike from_utf8_lossy
        // which corrupts non-UTF-8 with replacement chars (audit #9).
        let val: String = v.as_bytes().iter().map(|&b| b as char).collect();
        headers.push((k.as_str().to_ascii_lowercase(), val));
    }
    // CRITICAL #1: cap the body (16 MiB) → 413, instead of an unbounded
    // allocation per connection (remote memory-DoS). A truncated/aborted
    // body is a 400, NOT a silent empty-body 200.
    let body = match Limited::new(req.into_body(), max_body()).collect().await {
        Ok(c) => c.to_bytes(),
        Err(_) => return plain(StatusCode::PAYLOAD_TOO_LARGE, b"request entity too large"),
    };
    let parts = ReqParts {
        method,
        path,
        query_string,
        headers,
        body,
    };
    // Scope 1 (GIL): build the Request, call the handler, classify the result.
    // Wrapped in catch_unwind so a panic in the call (pyo3 conversion, etc.)
    // is a 500, not a worker kill (R7). `build_result` is sync (no .await).
    let classified = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        crate::gil::with_gil(|py| build_result(py, &app.locals, handler, parts))
    }));
    let dispatched = match classified {
        Ok(Ok(d)) => d,
        _ => return plain(StatusCode::INTERNAL_SERVER_ERROR, b"internal error"),
    };
    match dispatched {
        // SYNC handler: its body already ran inside `build_result` (GIL scope)
        // and its result is already serialized — no await, just emit the bytes.
        // Mirrors the `Dispatched::Value` success branch (`json_ok(json)`).
        Dispatched::Ready(json) => json_ok(json),
        // R3: async-gen → chunked streaming body (no per-chunk thread/GIL).
        Dispatched::Stream(gen, content_type) => {
            let body = crate::stream::stream_body(app.locals.clone(), gen, app.shutdown.clone());
            HResp::builder()
                .status(StatusCode::OK)
                .header("content-type", content_type)
                .body(body.boxed())
                .unwrap()
        }
        // Regular awaitable → await (GIL dropped) + serialize. R7:
        // catch_unwind so a panic anywhere in the future becomes a 500.
        Dispatched::Value(fut) => {
            use futures::FutureExt;
            let done = std::panic::AssertUnwindSafe(await_and_serialize(fut))
                .catch_unwind()
                .await;
            match done {
                Ok(Ok(json)) => json_ok(json),
                _ => plain(StatusCode::INTERNAL_SERVER_ERROR, b"internal error"),
            }
        }
    }
}

pub fn serve(py: Python<'_>, app: App, host: &str, port: u16) -> PyResult<()> {
    let addr: std::net::SocketAddr = format!("{host}:{port}")
        .parse()
        .map_err(|e| PyOSError::new_err(format!("bad bind addr: {e}")))?;
    let event_loop = app.locals.event_loop(py);

    // M18 Task#7: drive ASGI lifespan STARTUP on the server's own loop BEFORE
    // we bind the listener. The startup coroutine runs to completion here —
    // `@app.on_event`/root `lifespan=`/mounted sub-apps/`@app.lifetime_task`
    // daemons all init on THIS loop (the loop-identity invariant: a daemon
    // started via `asyncio.ensure_future` inside startup binds to `event_loop`,
    // which the accept loop below re-runs, so it lives for the process). If
    // startup RAISES we REFUSE TO SERVE: close the loop, propagate the PyErr,
    // and return WITHOUT binding a socket — never accept traffic on a
    // half-initialized app (uvicorn `startup.failed` parity). This is the SAME
    // proven `event_loop.run_until_complete(coro)` bridge `graceful_loop_teardown`
    // uses (no GIL-across-await: run_until_complete drives the loop and returns).
    if let Some(cb) = app.on_startup.as_ref() {
        if let Err(e) = run_lifespan_phase(py, &event_loop, cb, None) {
            graceful_loop_teardown(py, &event_loop);
            event_loop.call_method0("close").ok();
            return Err(e);
        }
    }

    let std_listener = match crate::socket::bind_reuseport(addr) {
        Ok(l) => l,
        Err(e) => {
            // Bind failed AFTER startup ran — tear startup back down (run
            // shutdown best-effort) so a port clash doesn't leak resources.
            if let Some(cb) = app.on_shutdown.as_ref() {
                let _ = run_lifespan_phase(py, &event_loop, cb, Some(drain_timeout()));
            }
            graceful_loop_teardown(py, &event_loop);
            event_loop.call_method0("close").ok();
            return Err(PyOSError::new_err(e.to_string()));
        }
    };
    std_listener.set_nonblocking(true).ok();
    let app = Arc::new(app);
    // Keep an Arc handle for the post-drain lifespan-shutdown call below: the
    // `server` future moves `app` in (`async move`), so we clone the (cheap,
    // refcount-bump) Arc here to reach `on_startup`/`on_shutdown` afterward.
    let app_for_shutdown = app.clone();

    // Bounded concurrent connections (domain-web). Acquire a permit BEFORE
    // accepting → backpressure lands in the kernel listen backlog, not in an
    // unbounded pile of spawned tasks.
    let max_conns = max_connections();
    let conn_limit = Arc::new(tokio::sync::Semaphore::new(max_conns));

    let server = async move {
        let listener = tokio::net::TcpListener::from_std(std_listener)
            .map_err(|e| PyOSError::new_err(e.to_string()))?;
        let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .map_err(|e| PyOSError::new_err(e.to_string()))?;
        // M7 T4.11: also catch SIGINT (Ctrl+C) so `python -c "lancelot.run(...)"`
        // in a dev shell exits cleanly without leaving the asyncio loop pinned.
        // The graceful_break! macro covers both signals identically — same drain
        // path, same bounded timeout. (Without this, Ctrl+C ends up killing the
        // Python process with an unclean asyncio teardown — LanceDB pattern,
        // executor.rs:88-115 equivalent.)
        let mut sigint = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())
            .map_err(|e| PyOSError::new_err(e.to_string()))?;
        // Graceful-shutdown helper (B1 — drain, NOT Robyn-style hard-kill).
        // First wake in-flight R3 stream pumps so they stop pulling (audit #2),
        // then DRAIN: acquire ALL connection permits — satisfiable only once
        // every in-flight connection has released its permit (i.e. finished) —
        // bounded by `drain_timeout()`. We're still inside `run_until_complete`,
        // so the asyncio loop keeps pumping and in-flight handlers actually
        // progress to completion instead of being cancelled at close (the V1.4
        // bug: in-flight requests 500'd on SIGTERM). A stuck handler can't block
        // shutdown past the deadline.
        macro_rules! graceful_break {
            () => {{
                app.shutdown.notify_waiters();
                let drain = conn_limit.clone().acquire_many_owned(max_conns as u32);
                let _ = tokio::time::timeout(drain_timeout(), drain).await;
                break;
            }};
        }
        // M7 T4.2 audit (ZED.md:4826-4861 + 10994-11030 select_biased! pattern):
        // both selects below already use `biased;` so SIGTERM/SIGINT are
        // compile-time prioritized over the accept_loop — the Zed pattern is
        // satisfied at this site. The `scopeguard::defer!` Drop-guard half of
        // the pattern (auto-cancel oneshot senders on Drop) does NOT apply
        // here because Lancelot's per-connection task doesn't hold any
        // oneshots (`_permit` is the only resource and RAII handles it).
        // If T1.1 spawn_child grows oneshot wiring later, add scopeguard at
        // THAT site.
        loop {
            // Acquire a connection permit — but keep SIGTERM responsive even
            // when saturated (don't block shutdown waiting for a free slot).
            let permit = tokio::select! {
                biased;
                _ = sigterm.recv() => graceful_break!(),
                _ = sigint.recv() => graceful_break!(),
                p = conn_limit.clone().acquire_owned() => match p {
                    Ok(p) => p,
                    Err(_) => break, // semaphore closed
                },
            };
            // Have a slot; accept — still abort promptly on SIGTERM/SIGINT.
            let (stream, _) = tokio::select! {
                biased;
                _ = sigterm.recv() => { drop(permit); graceful_break!(); }
                _ = sigint.recv() => { drop(permit); graceful_break!(); }
                accepted = listener.accept() => match accepted {
                    Ok(v) => v,
                    Err(_) => continue, // permit drops here → slot freed
                },
            };
            {
                let app = app.clone();
                tokio::spawn(async move {
                    // Permit held for the connection's whole lifetime;
                    // released here on close → frees a slot for accept.
                    let _permit = permit;
                    let io = TokioIo::new(stream);
                    let svc = hyper::service::service_fn(move |r| {
                        let app = app.clone();
                        async move { Ok::<_, std::convert::Infallible>(handle(app, r).await) }
                    });
                    // HTTP/1.1 ONLY (deliberate, audit MEDIUM-1). The prior
                    // `AutoBuilder` auto-negotiated HTTP/2 cleartext (h2c) on
                    // the same socket, but the slow-loris `header_read_timeout`
                    // below applies only to the h1 path — so an h2c client got
                    // an UNGUARDED connection. This server binds cleartext TCP
                    // with no TLS (it sits behind a TLS-terminating proxy);
                    // real h2 arrives via TLS+ALPN at that proxy, and h2c is
                    // neither a goal nor tested here. Serving an unhardened,
                    // untested protocol path violates the no-bullshit rule, so
                    // we restrict to h1 explicitly. If h2 becomes a goal it is
                    // a deliberate addition WITH its own connection limits.
                    //
                    // R6: slow-loris hardening — cap how long a client can
                    // dribble request headers before the connection is dropped
                    // (Granian/nginx-class default; closes design/01:400-402,
                    // SYNTHESIS.md:102 — Blazeio had no header deadline, Robyn's
                    // actix had it set to 0 disabling the gate).
                    let mut builder = hyper::server::conn::http1::Builder::new();
                    builder
                        .timer(TokioTimer::new())
                        .header_read_timeout(std::time::Duration::from_millis(header_timeout_ms()));
                    let _ = builder.serve_connection(io, svc).await;
                });
            }
        }
        Ok(())
    };

    // Drives the asyncio loop + tokio hyper server together until SIGTERM.
    // Returns AFTER the `graceful_break!` connection drain completes (or the
    // loop errors), so by here in-flight requests have finished.
    let run = pyo3_async_runtimes::tokio::run_until_complete(event_loop.clone(), server);
    // M18 Task#7: drive ASGI lifespan SHUTDOWN AFTER the connection drain and
    // BEFORE the generic loop teardown. Bounded by `drain_timeout()` /
    // LANCELOT_DRAIN_SECS (via `asyncio.wait_for`) so a wedged user shutdown
    // (e.g. a DB pool-close that hangs) can't block process exit — worse than
    // the old no-op. This sends `lifespan.shutdown` to the long-lived
    // `_lifespan` task, which cancels `@app.lifetime_task` daemons (their
    // `finally` runs), tears down mounted sub-apps (LIFO), then exits the root
    // `lifespan=` CM — so the `_lifespan` task is DONE before
    // graceful_loop_teardown runs (no "Task was destroyed but pending"). The
    // loop is still live here (we haven't closed it), so run_until_complete
    // works. Best-effort: a shutdown error is logged via the failure message
    // and swallowed (we're already exiting). Skipped if startup never ran.
    if app_for_shutdown.on_startup.is_some() {
        if let Some(cb) = app_for_shutdown.on_shutdown.as_ref() {
            let _ = run_lifespan_phase(py, &event_loop, cb, Some(drain_timeout()));
        }
    }
    // Graceful asyncio teardown before close (audit HIGH-2): cancel lingering
    // tasks — chiefly in-flight stream `_drain` coros, which `into_future`
    // bridging does NOT cancel — and finalize async generators, so user
    // `finally`/`aclose` blocks RUN (resource release) instead of a noisy
    // "Task was destroyed but it is pending!" on close. Best-effort + bounded.
    graceful_loop_teardown(py, &event_loop);
    event_loop.call_method0("close").ok();
    run
}

/// M18 Task#7: drive one ASGI lifespan phase (startup or shutdown) on the
/// server's OWN asyncio loop. `callback` is a zero-arg Python callable that
/// returns a coroutine (built in `app.py::_make_lifespan_callbacks`). When
/// `bounded` is `Some(d)` (shutdown), the coroutine is wrapped in
/// `asyncio.wait_for(coro, d.as_secs_f64())` so a wedged user shutdown can't
/// block process exit; startup passes `None` (no bound — a hung startup never
/// becomes ready, the operator's problem to fix, same as uvicorn).
///
/// This reuses the proven `event_loop.run_until_complete(coro)` bridge that
/// `graceful_loop_teardown` already uses: `run_until_complete` is a synchronous
/// Python call that drives the loop until the coroutine resolves and returns
/// its result — there is NO Rust `.await` here, so the GIL is never held across
/// an await (the `await_holding_lock` invariant is trivially satisfied). A
/// startup failure surfaces as a `PyErr` (the coroutine raises) and is returned
/// to the caller, which refuses to serve.
fn run_lifespan_phase(
    py: Python<'_>,
    event_loop: &Bound<'_, PyAny>,
    callback: &Py<PyAny>,
    bounded: Option<std::time::Duration>,
) -> PyResult<()> {
    // Build the coroutine: callback() -> coroutine object.
    let coro = callback.bind(py).call0()?;
    let driver = match bounded {
        Some(d) => {
            // asyncio.wait_for(coro, timeout) — a coroutine itself.
            let asyncio = py.import("asyncio")?;
            asyncio.call_method1("wait_for", (coro, d.as_secs_f64()))?
        }
        None => coro,
    };
    event_loop.call_method1("run_until_complete", (driver,))?;
    Ok(())
}

/// Cancel pending tasks + run their cancellation (and async-gen `aclose`) to
/// completion, bounded so a slow user `finally` can't wedge shutdown. All
/// best-effort: any error here just means we proceed to `close()` (we're
/// already shutting down). Mirrors CPython's `asyncio.run()` finalizer.
fn graceful_loop_teardown(py: Python<'_>, event_loop: &Bound<'_, PyAny>) {
    let Ok(asyncio) = py.import("asyncio") else {
        return;
    };
    // tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
    if let Ok(tasks) = asyncio.call_method1("all_tasks", (event_loop,)) {
        let pending: Vec<Bound<'_, PyAny>> = tasks
            .try_iter()
            .into_iter()
            .flatten()
            .flatten()
            .filter(|t| !matches!(t.call_method0("done").and_then(|d| d.is_truthy()), Ok(true)))
            .collect();
        for t in &pending {
            let _ = t.call_method0("cancel");
        }
        if !pending.is_empty() {
            // loop.run_until_complete(
            //     asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 2.0))
            let _ = (|| -> PyResult<()> {
                let args = pyo3::types::PyTuple::new(py, &pending)?;
                let kwargs = pyo3::types::PyDict::new(py);
                kwargs.set_item("return_exceptions", true)?;
                let gather = asyncio.call_method("gather", args, Some(&kwargs))?;
                let bounded = asyncio.call_method1("wait_for", (gather, 2.0_f64))?;
                event_loop.call_method1("run_until_complete", (bounded,))?;
                Ok(())
            })();
        }
    }
    // loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), 2.0))
    // Bounded (audit F1): a pathological async-gen `aclose` that blocks forever
    // must not wedge process shutdown — SIGTERM always exits within the bound.
    if let Ok(coro) = event_loop.call_method0("shutdown_asyncgens") {
        let _ = (|| -> PyResult<()> {
            let bounded = asyncio.call_method1("wait_for", (coro, 2.0_f64))?;
            event_loop.call_method1("run_until_complete", (bounded,))?;
            Ok(())
        })();
    }
}
