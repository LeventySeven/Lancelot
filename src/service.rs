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
use crate::dispatch::call_async;
use crate::router::Router;
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{body::Incoming, Request as HReq, Response as HResp, StatusCode};
use hyper_util::rt::{TokioExecutor, TokioIo};
use hyper_util::server::conn::auto::Builder as AutoBuilder;
use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;
use pyo3_async_runtimes::TaskLocals;
use std::sync::Arc;

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
    let slot = match app.router.route(&method, &path) {
        Some(s) => s,
        None => return plain(StatusCode::NOT_FOUND, b"not found"),
    };
    let body = match req.into_body().collect().await {
        Ok(c) => c.to_bytes(),
        Err(_) => Bytes::new(),
    };
    let handler = crate::gil::with_gil(|py| app.handlers[slot].clone_ref(py));
    match call_async(app.locals.clone(), handler, body).await {
        Ok(json) => HResp::builder()
            .status(StatusCode::OK)
            .header("content-type", "application/json")
            .body(Full::new(Bytes::from(json)))
            .unwrap(),
        Err(_) => plain(StatusCode::INTERNAL_SERVER_ERROR, b"internal error"),
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
                        let _ = AutoBuilder::new(TokioExecutor::new())
                            .serve_connection(io, svc)
                            .await;
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
