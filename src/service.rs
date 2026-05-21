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
use crate::dispatch::{call_async, ReqParts};
use crate::router::Router;
use bytes::Bytes;
use http_body_util::{BodyExt, Full, Limited};
use hyper::{body::Incoming, Request as HReq, Response as HResp, StatusCode};
use hyper_util::rt::{TokioExecutor, TokioIo, TokioTimer};
use hyper_util::server::conn::auto::Builder as AutoBuilder;
use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;
use pyo3_async_runtimes::TaskLocals;
use std::sync::Arc;

/// Max request body the production path will buffer before returning 413
/// (audit CRITICAL #1 — the M0 path collected unbounded bodies, a trivial
/// remote memory-DoS). Matches the Python ASGI seam's _MAX_BODY (16 MiB).
const MAX_BODY: usize = 16 * 1024 * 1024;

pub struct App {
    pub router: Router,
    pub handlers: Vec<Py<PyAny>>, // slot -> handler
    pub locals: TaskLocals,       // the server's asyncio loop + context
}

fn plain(status: StatusCode, body: &'static [u8]) -> HResp<Full<Bytes>> {
    HResp::builder()
        .status(status)
        .body(Full::new(Bytes::from_static(body)))
        .unwrap()
}

async fn handle(app: Arc<App>, req: HReq<Incoming>) -> HResp<Full<Bytes>> {
    let method = req.method().as_str().to_string();
    let path = req.uri().path().to_string();
    // R2: capture the REAL query string + headers (was dropped in M0).
    let query_string = req.uri().query().unwrap_or("").to_string();
    let headers: Vec<(String, String)> = req
        .headers()
        .iter()
        .map(|(k, v)| {
            (
                k.as_str().to_ascii_lowercase(),
                String::from_utf8_lossy(v.as_bytes()).into_owned(),
            )
        })
        .collect();
    let slot = match app.router.route(&method, &path) {
        Some(s) => s,
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
    // R7: catch_unwind at the FFI seam — any panic in the dispatch
    // future (Python code, pyo3 conversions, anywhere) becomes a 500
    // INSTEAD of killing the worker. Closes the documented Robyn defect
    // (deep/04:17 — panic=abort with no catch_unwind kills the worker
    // with no supervisor). `panic = "unwind"` is set in Cargo.toml so
    // this is actionable.
    use futures::FutureExt;
    let dispatched = std::panic::AssertUnwindSafe(
        call_async(app.locals.clone(), handler, parts)
    ).catch_unwind().await;
    match dispatched {
        Ok(Ok(json)) => HResp::builder()
            .status(StatusCode::OK)
            .header("content-type", "application/json")
            .body(Full::new(Bytes::from(json)))
            .unwrap(),
        Ok(Err(_)) => plain(StatusCode::INTERNAL_SERVER_ERROR, b"internal error"),
        Err(_panicked) => plain(
            StatusCode::INTERNAL_SERVER_ERROR, b"internal error"),
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

    let server = async move {
        let listener = tokio::net::TcpListener::from_std(std_listener)
            .map_err(|e| PyOSError::new_err(e.to_string()))?;
        let mut sigterm =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .map_err(|e| PyOSError::new_err(e.to_string()))?;
        loop {
            tokio::select! {
                _ = sigterm.recv() => break, // graceful: stop accepting
                accepted = listener.accept() => {
                    let (stream, _) = match accepted { Ok(v) => v, Err(_) => continue };
                    let app = app.clone();
                    tokio::spawn(async move {
                        let io = TokioIo::new(stream);
                        let svc = hyper::service::service_fn(move |r| {
                            let app = app.clone();
                            async move {
                                Ok::<_, std::convert::Infallible>(handle(app, r).await)
                            }
                        });
                        // R6: slow-loris hardening — cap how long a client
                        // can dribble request headers before the connection
                        // is dropped (Granian/nginx-class default; closes
                        // design/01:400-402, SYNTHESIS.md:102 — Blazeio
                        // had no header deadline, Robyn's actix had it set
                        // to 0 disabling the gate).
                        let mut builder = AutoBuilder::new(TokioExecutor::new());
                        builder.http1().timer(TokioTimer::new())
                            .header_read_timeout(
                                std::time::Duration::from_secs(15));
                        let _ = builder.serve_connection(io, svc).await;
                    });
                }
            }
        }
        Ok(())
    };

    // Drives the asyncio loop + tokio hyper server together until SIGTERM.
    let run = pyo3_async_runtimes::tokio::run_until_complete(event_loop.clone(), server);
    event_loop.call_method0("close").ok();
    run
}
