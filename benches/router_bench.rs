//! Criterion bench for the M3 routing hot path (R1 const-tier + R2 dynamic
//! routing). Pure-Rust — no pyo3/GIL — so it measures the matchit lookup the
//! production accept loop runs per request. The plan discipline requires a
//! criterion bench per Rust swap (docs/plans/2026-05-20-...:Discipline).
use criterion::{black_box, criterion_group, criterion_main, Criterion};
use lancelot::router::Router;

fn build_router() -> Router {
    let mut r = Router::new();
    // A representative mix: static-shape (const-tier candidates) + param routes.
    r.add("GET", "/livez", 0).unwrap();
    r.add("GET", "/readyz", 1).unwrap();
    r.add("GET", "/openapi.json", 2).unwrap();
    r.add("GET", "/users/{id}", 3).unwrap();
    r.add("GET", "/users/{id}/items/{item_id}", 4).unwrap();
    r.add("POST", "/echo", 5).unwrap();
    r.add("GET", "/", 6).unwrap();
    r
}

fn bench_router(c: &mut Criterion) {
    let r = build_router();
    // Static-shape lookup (the /livez, /openapi.json hot path).
    c.bench_function("route_static", |b| {
        b.iter(|| r.route(black_box("GET"), black_box("/openapi.json")))
    });
    // Param lookup (the dynamic tier).
    c.bench_function("route_param", |b| {
        b.iter(|| r.route(black_box("GET"), black_box("/users/42/items/99")))
    });
    // 404 (miss) — the negative path also matters for cost.
    c.bench_function("route_miss", |b| {
        b.iter(|| r.route(black_box("GET"), black_box("/nope/nope")))
    });
    // 405 path: known path, wrong method.
    c.bench_function("route_wrong_method", |b| {
        b.iter(|| r.route(black_box("DELETE"), black_box("/echo")))
    });
}

criterion_group!(benches, bench_router);
criterion_main!(benches);
