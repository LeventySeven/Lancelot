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
use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use hyper_util::server::conn::auto::Builder as AutoBuilder;
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
/// remote memory-DoS). Matches the Python ASGI seam's _MAX_BODY (16 MiB).
const MAX_BODY: usize = 16 * 1024 * 1024;

/// Max concurrent connections the accept loop will serve (domain-web skill:
/// an unbounded `loop { accept; spawn }` is a DoS-amplification vector — a
/// connection flood spawns unbounded tasks). A bounded Semaphore acquired
/// BEFORE accept gives kernel-backlog backpressure (the OS buffers, then
/// refuses SYNs) rather than buffering accepted-but-unserved connections in
/// memory. Permit is held for the connection's lifetime, released on close.
const MAX_CONNECTIONS: usize = 1024;

pub struct App {
    pub router: Router,
    pub handlers: Vec<Py<PyAny>>, // slot -> handler
    pub locals: TaskLocals,       // the server's asyncio loop + context
    // Notified on graceful shutdown (SIGTERM) so in-flight R3 stream pumps
    // stop pulling before the event loop closes (audit #2).
    pub shutdown: std::sync::Arc<tokio::sync::Notify>,
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

fn json_ok(body: Vec<u8>) -> HResp<RespBody> {
    HResp::builder()
        .status(StatusCode::OK)
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from(body)).map_err(|e| match e {}).boxed())
        .unwrap()
}

async fn handle(app: Arc<App>, req: HReq<Incoming>) -> HResp<RespBody> {
    let method = req.method().as_str().to_string();
    let path = req.uri().path().to_string();
    // R2: capture the REAL query string + headers (was dropped in M0).
    let query_string = req.uri().query().unwrap_or("").to_string();
    let headers: Vec<(String, String)> = req
        .headers()
        .iter()
        .map(|(k, v)| {
            // Starlette decodes header values as latin-1 (1 byte → 1 char),
            // which is lossless for arbitrary bytes — unlike from_utf8_lossy
            // which corrupts non-UTF-8 with replacement chars (audit #9).
            let val: String = v.as_bytes().iter().map(|&b| b as char).collect();
            (k.as_str().to_ascii_lowercase(), val)
        })
        .collect();
    let slot = match app.router.route(&method, &path) {
        Some(s) => s,
        // 405 if the path exists under another method; 404 otherwise
        // (HTTP semantics / FastAPI parity — domain-web).
        None if app.router.matches_any_method(&path) => {
            return plain(StatusCode::METHOD_NOT_ALLOWED, b"method not allowed")
        }
        None => return plain(StatusCode::NOT_FOUND, b"not found"),
    };
    // Audit #5: bounds-checked slot lookup (router/handlers desync would
    // otherwise panic OUTSIDE catch_unwind). Should be unreachable given the
    // 1:1 build in lib.rs, but enforce rather than assume.
    let handler = match app.handlers.get(slot) {
        Some(h) => crate::gil::with_gil(|py| h.clone_ref(py)),
        None => return plain(StatusCode::INTERNAL_SERVER_ERROR, b"internal error"),
    };
    // CRITICAL #1: cap the body (16 MiB) → 413, instead of an unbounded
    // allocation per connection (remote memory-DoS). A truncated/aborted
    // body is a 400, NOT a silent empty-body 200.
    let body = match Limited::new(req.into_body(), MAX_BODY).collect().await {
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
    let std_listener =
        crate::socket::bind_reuseport(addr).map_err(|e| PyOSError::new_err(e.to_string()))?;
    std_listener.set_nonblocking(true).ok();
    let event_loop = app.locals.event_loop(py);
    let app = Arc::new(app);

    // Bounded concurrent connections (domain-web). Acquire a permit BEFORE
    // accepting → backpressure lands in the kernel listen backlog, not in an
    // unbounded pile of spawned tasks.
    let conn_limit = Arc::new(tokio::sync::Semaphore::new(MAX_CONNECTIONS));

    let server = async move {
        let listener = tokio::net::TcpListener::from_std(std_listener)
            .map_err(|e| PyOSError::new_err(e.to_string()))?;
        let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .map_err(|e| PyOSError::new_err(e.to_string()))?;
        // Graceful-shutdown helper: drain in-flight R3 stream pumps before the
        // event loop closes (audit #2), then signal the loop to stop.
        macro_rules! graceful_break {
            () => {{
                app.shutdown.notify_waiters();
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
                break;
            }};
        }
        loop {
            // Acquire a connection permit — but keep SIGTERM responsive even
            // when saturated (don't block shutdown waiting for a free slot).
            let permit = tokio::select! {
                biased;
                _ = sigterm.recv() => graceful_break!(),
                p = conn_limit.clone().acquire_owned() => match p {
                    Ok(p) => p,
                    Err(_) => break, // semaphore closed
                },
            };
            // Have a slot; accept — still abort promptly on SIGTERM.
            let (stream, _) = tokio::select! {
                biased;
                _ = sigterm.recv() => { drop(permit); graceful_break!(); }
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
                    // R6: slow-loris hardening — cap how long a client
                    // can dribble request headers before the connection
                    // is dropped (Granian/nginx-class default; closes
                    // design/01:400-402, SYNTHESIS.md:102 — Blazeio
                    // had no header deadline, Robyn's actix had it set
                    // to 0 disabling the gate).
                    let mut builder = AutoBuilder::new(TokioExecutor::new());
                    builder
                        .http1()
                        .timer(TokioTimer::new())
                        .header_read_timeout(std::time::Duration::from_secs(15));
                    let _ = builder.serve_connection(io, svc).await;
                });
            }
        }
        Ok(())
    };

    // Drives the asyncio loop + tokio hyper server together until SIGTERM.
    let run = pyo3_async_runtimes::tokio::run_until_complete(event_loop.clone(), server);
    event_loop.call_method0("close").ok();
    run
}
