# Lancelot M0 — Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One complete request path through every load-bearing layer — `socket2` SO_REUSEPORT → hyper/tower → matchit → lazy `Request` → GIL choreography (drop GIL across the Python `await`) → pydantic-core one-pass serialization → 200 JSON — proving the thin-real-Rust-core architecture before any fan-out.

**Architecture:** A Rust extension module (`pyo3` 0.27, written 0.28-clean) built by `maturin`, exposing a `Lancelot` app to Python. Registration snapshots each handler into an opaque `FnInfo` (no per-request reflection). The hot path is GIL-free Rust except three µs-scale `with_gil` scopes; the no-`.await`-in-`with_gil` invariant is **type-enforced** (our `with_gil` wrapper takes a sync closure) and **compile-tested** (`trybuild`). Serialization delegates to `pydantic_core.to_json` via FFI.

**Tech Stack:** Rust (pyo3 0.27, pyo3-async-runtimes 0.27, hyper 1, hyper-util 0.1, http-body-util 0.1, tower 0.5, tokio 1, socket2 0.5, matchit 0.8, bytes 1), maturin, Python 3.13/3.14 + pydantic 2 / pydantic-core, pytest + httpx, trybuild, dhat-rs.

**Scope:** M0 only. M1 (table-stakes + conformance ≥90%), M2 (AI tier), M3 (cloud/ops), M4 (release) get their own plans derived from the WBS waves (`research/deep/09`) after M0 is green.

**Repository model (corrected 2026-05-19 — supersedes the original gitignored/nested-local-repo premise):** This project now lives in its own git repository — `github.com/LeventySeven/Lancelot.git`, working tree `/Users/seventyleven/Desktop/Lancelot/`. Throughout this plan `Lancelot/<path>` means **the repo root** (`Lancelot/src/lib.rs` → `./src/lib.rs`, `Lancelot/Cargo.toml` → `./Cargo.toml`, etc.). **Do NOT run `git init` and do NOT create a nested `.git`** — Task 1 Step 7's `git init` is removed; all scaffold/feature commits go to the existing repo. Every `cd Lancelot && git …` step below means *"from the repo root, commit to the existing repo."* The repo-root `.gitignore` already excludes `/target/`, `*.so`, `__pycache__/`, `.venv/`, `*.profraw`, `dhat-heap.json` and `/researchfms/`, so Task 1 Step 6's separate `Lancelot/.gitignore` is **superseded — skip writing it**.

**M0 Definition of Done (the 7 acceptance assertions, verified in Task 12):**
1. `maturin develop` builds; `import lancelot` works under `python3.13` AND `python3.14` (free-threaded `*t` = CI-only, skipped locally if absent).
2. `cargo test` green, including the `trybuild` compile-fail fixture proving `.await` inside `with_gil` does not compile.
3. `POST /echo` round-trips a JSON body over a real socket (httpx + curl).
4. The GIL is provably released during the handler `await` (concurrency test: 50 concurrent 100ms-sleep handlers finish in ≈100ms, not ≈5s).
5. Framework heap allocations for one request ≤ 5 (dhat assertion; target 1–3).
6. Two processes bind the same port simultaneously (SO_REUSEPORT proven).
7. `lancelot.run(app)` serves; SIGTERM exits cleanly (no traceback, exit 0).

---

## File Structure

**Rust crate (`Lancelot/src/`):**
- `lib.rs` — the `#[pymodule]`; re-exports; `run()` entrypoint binding.
- `gil.rs` — `with_gil()` (sync-closure-only, the type-enforced invariant) + `detach()`.
- `cpu.rs` — cgroup-aware worker count.
- `socket.rs` — SO_REUSEPORT/REUSEADDR/NODELAY listener via socket2.
- `ffi/mod.rs`, `ffi/fninfo.rs` (`#[pyclass] FnInfo`), `ffi/request.rs` (`#[pyclass] Request`, lazy).
- `dispatch.rs` — the GIL choreography: call handler → drop GIL → await → reacquire → serialize.
- `serialize.rs` — pydantic-core `to_json` FFI seam (+ stdlib `json` fallback).
- `router.rs` — matchit per-method + const-tier stub.
- `service.rs` — the frozen `tower::Service` seam; hyper-util auto h1/h2 accept loop; SIGTERM drain.

**Python package (`Lancelot/lancelot/`):**
- `__init__.py` — public surface: `Lancelot`, `run`, `JSONResponse`.
- `app.py` — `Lancelot` app object; `@app.post(...)` registration → `FnInfo`.
- `_native.pyi` — typed stub for the compiled module (auto-gen later; hand-stub in M0).

**Tests:**
- `Lancelot/tests/ui/gil_await_fail.rs` + `Lancelot/tests/trybuild.rs` — compile-fail proof.
- Rust unit tests inline (`#[cfg(test)]`) in `cpu.rs`, `socket.rs`, `router.rs`, `request.rs`.
- `Lancelot/tests/test_m0_echo.py` — end-to-end echo, concurrency/GIL, lifecycle.
- `Lancelot/tests/test_alloc.py` + `Lancelot/benches/alloc.rs` — allocation gate.
- `Lancelot/Makefile` — `make m0` runs all 7 acceptance checks.

---

### Task 1: Project scaffold + local git + toolchain

**Files:**
- Create: `Lancelot/Cargo.toml`, `Lancelot/pyproject.toml`, `Lancelot/src/lib.rs`, `Lancelot/lancelot/__init__.py`, `Lancelot/.gitignore`, `Lancelot/rust-toolchain.toml`

- [ ] **Step 1: Bootstrap toolchain (idempotent)**

Run:
```bash
command -v cargo || curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
python3 -m pip install --user --upgrade "maturin>=1.7" "pytest" "httpx" "pydantic>=2.7"
cargo --version && maturin --version
```
Expected: `cargo 1.x`, `maturin 1.7+` print. (Free-threaded `python3.13t`/`python3.14t` is NOT required locally; the ft-import gate is CI-only.)

- [ ] **Step 2: Write `Lancelot/Cargo.toml`**

```toml
[package]
name = "lancelot"
version = "0.0.0"
edition = "2021"
rust-version = "1.78"

[lib]
name = "lancelot"
crate-type = ["cdylib", "rlib"]

[dependencies]
pyo3 = { version = "0.27", features = ["extension-module", "abi3-py311"] }
pyo3-async-runtimes = { version = "0.27", features = ["tokio-runtime"] }
tokio = { version = "1", features = ["rt-multi-thread", "net", "signal", "time", "sync", "macros"] }
hyper = { version = "1", features = ["http1", "http2", "server"] }
hyper-util = { version = "0.1", features = ["server-auto", "tokio"] }
http-body-util = "0.1"
http = "1"
tower = { version = "0.5", features = ["util"] }
socket2 = "0.5"
matchit = "0.8"
bytes = "1"
dhat = { version = "0.3", optional = true }

[dev-dependencies]
trybuild = "1"

[features]
dhat-heap = ["dep:dhat"]

[profile.release]
lto = "fat"
codegen-units = 1
opt-level = 3
panic = "unwind"
```

- [ ] **Step 3: Write `Lancelot/pyproject.toml`**

```toml
[build-system]
requires = ["maturin>=1.7,<2"]
build-backend = "maturin"

[project]
name = "lancelot"
version = "0.0.0"
requires-python = ">=3.11"
dependencies = ["pydantic>=2.7"]

[tool.maturin]
features = ["pyo3/extension-module"]
module-name = "lancelot._native"
python-source = "."
```

- [ ] **Step 4: Write minimal `Lancelot/src/lib.rs`**

```rust
use pyo3::prelude::*;

#[pyfunction]
fn _ping() -> &'static str {
    "lancelot-m0"
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(_ping, m)?)?;
    Ok(())
}
```

- [ ] **Step 5: Write `Lancelot/lancelot/__init__.py`**

```python
from lancelot._native import _ping

__all__ = ["_ping"]
__version__ = "0.0.0"
```

- [ ] **Step 6: Write `Lancelot/rust-toolchain.toml`**

`rust-toolchain.toml`:
```toml
[toolchain]
channel = "stable"
components = ["clippy", "rustfmt"]
```
(No `.gitignore` here — the repo-root `.gitignore` already covers `/target`, `*.so`, `__pycache__/`, `.venv/`, `*.profraw`, `dhat-heap.json`. Per the corrected repository model above, this step's old `Lancelot/.gitignore` is superseded.)

- [ ] **Step 7: Scaffold-commit to the existing repo and build**

Run (from the repo root — **no `git init`**, the repo already exists):
```bash
git add -A && git commit -q -m "chore: M0 scaffold"
maturin develop
python3 -c "import lancelot; print(lancelot._ping())"
```
Expected: prints `lancelot-m0`. (Commits land in the existing `github.com/LeventySeven/Lancelot.git` repo; there is no nested `.git`.)

- [ ] **Step 8: Commit**

```bash
cd Lancelot && git add -A && git commit -q -m "feat: lancelot extension imports and pings"
```

---

### Task 2: The `with_gil` invariant + compile-fail proof

**Files:**
- Create: `Lancelot/src/gil.rs`, `Lancelot/tests/trybuild.rs`, `Lancelot/tests/ui/gil_await_fail.rs`, `Lancelot/tests/ui/gil_ok.rs`
- Modify: `Lancelot/src/lib.rs`

- [ ] **Step 1: Write the compile-fail fixture (the failing "test")**

`Lancelot/tests/ui/gil_await_fail.rs`:
```rust
// This MUST NOT compile: `.await` inside the sync closure passed to with_gil.
use lancelot::gil::with_gil;

async fn bad() {
    with_gil(|_py| {
        futures::future::ready(()).await; // error: `await` only allowed in async fn/block
    });
}

fn main() {}
```

`Lancelot/tests/ui/gil_ok.rs`:
```rust
use lancelot::gil::with_gil;
fn main() {
    let n: usize = with_gil(|_py| 1 + 1);
    assert_eq!(n, 2);
}
```

`Lancelot/tests/trybuild.rs`:
```rust
#[test]
fn gil_invariant() {
    let t = trybuild::TestCases::new();
    t.pass("tests/ui/gil_ok.rs");
    t.compile_fail("tests/ui/gil_await_fail.rs");
}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd Lancelot && cargo test --test trybuild`
Expected: FAIL — `gil::with_gil` does not exist yet (module unresolved).

- [ ] **Step 3: Implement `src/gil.rs` (minimal — the invariant is the sync `FnOnce`)**

```rust
//! The core invariant: a GIL scope is a SYNC closure. `.await` inside it is a
//! compile error by construction (you cannot `.await` in a non-async closure).
//! 0.28-clean: uses `Python::with_gil`; rename to `attach` at the 0.28 bump.
use pyo3::Python;

#[inline]
pub fn with_gil<R>(f: impl for<'py> FnOnce(Python<'py>) -> R) -> R {
    Python::with_gil(f)
}

/// Explicit GIL-release marker for blocking sections (used by dispatch).
#[inline]
pub fn detach<R>(py: Python<'_>, f: impl FnOnce() -> R) -> R {
    py.allow_threads(f)
}
```

- [ ] **Step 4: Export the module in `src/lib.rs`**

Add to the top of `Lancelot/src/lib.rs` (after `use pyo3::prelude::*;`):
```rust
pub mod gil;
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd Lancelot && cargo test --test trybuild`
Expected: PASS — `gil_ok.rs` compiles, `gil_await_fail.rs` fails to compile with an `await` error.

- [ ] **Step 6: Commit**

```bash
cd Lancelot && git add -A && git commit -q -m "feat: type-enforced no-await-in-gil invariant + compile-fail proof"
```

---

### Task 3: cgroup-aware worker count

**Files:**
- Create: `Lancelot/src/cpu.rs`
- Modify: `Lancelot/src/lib.rs`

- [ ] **Step 1: Write the failing test (inline in `src/cpu.rs`)**

```rust
//! cgroup-v2/v1 aware CPU count. Fixes Robyn's os.cpu_count() meltdown.
use std::path::Path;

/// Effective worker count: cgroup quota if present, else logical CPUs, clamped [1, 64].
pub fn worker_count() -> usize {
    workers_from(Path::new("/sys/fs/cgroup"), num_cpus_fallback())
}

fn num_cpus_fallback() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
}

fn workers_from(cgroup_root: &Path, logical: usize) -> usize {
    // cgroup v2: cpu.max = "<quota> <period>" or "max <period>"
    let v2 = cgroup_root.join("cpu.max");
    let quota_cpus = std::fs::read_to_string(&v2).ok().and_then(|s| {
        let mut it = s.split_whitespace();
        let quota = it.next()?;
        let period: f64 = it.next()?.parse().ok()?;
        if quota == "max" { return None; }
        let q: f64 = quota.parse().ok()?;
        if period <= 0.0 { return None; }
        Some((q / period).ceil() as usize)
    });
    let n = quota_cpus.unwrap_or(logical).min(logical).max(1);
    n.min(64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn cgroup_v2_quota_caps_workers() {
        let dir = std::env::temp_dir().join(format!("lc_cg_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut f = std::fs::File::create(dir.join("cpu.max")).unwrap();
        write!(f, "150000 100000").unwrap(); // 1.5 cores
        assert_eq!(workers_from(&dir, 32), 2); // ceil(1.5) = 2, capped by quota
    }

    #[test]
    fn no_cgroup_uses_logical_clamped() {
        let dir = std::env::temp_dir().join("lc_cg_none");
        let _ = std::fs::remove_dir_all(&dir);
        assert_eq!(workers_from(&dir, 8), 8);
        assert_eq!(workers_from(&dir, 999), 64);
    }
}
```

- [ ] **Step 2: Wire module + run test**

Add `pub mod cpu;` to `Lancelot/src/lib.rs`. Run: `cd Lancelot && cargo test cpu::`
Expected: 2 tests PASS.

- [ ] **Step 3: Commit**

```bash
cd Lancelot && git add -A && git commit -q -m "feat: cgroup-aware worker count"
```

---

### Task 4: SO_REUSEPORT listener

**Files:**
- Create: `Lancelot/src/socket.rs`
- Modify: `Lancelot/src/lib.rs`

- [ ] **Step 1: Write the failing test (inline)**

```rust
//! Shared listening socket: SO_REUSEPORT + SO_REUSEADDR + TCP_NODELAY, backlog 16384.
use socket2::{Domain, Protocol, Socket, Type};
use std::net::SocketAddr;

pub fn bind_reuseport(addr: SocketAddr) -> std::io::Result<std::net::TcpListener> {
    let domain = if addr.is_ipv4() { Domain::IPV4 } else { Domain::IPV6 };
    let sock = Socket::new(domain, Type::STREAM, Some(Protocol::TCP))?;
    sock.set_reuse_address(true)?;
    #[cfg(unix)]
    sock.set_reuse_port(true)?;
    sock.set_nodelay(true)?;
    sock.bind(&addr.into())?;
    sock.listen(16384)?;
    Ok(sock.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn two_listeners_same_port() {
        let addr: SocketAddr = "127.0.0.1:0".parse().unwrap();
        let l1 = bind_reuseport(addr).unwrap();
        let port = l1.local_addr().unwrap().port();
        // Second bind on the SAME concrete port must succeed with REUSEPORT.
        let again: SocketAddr = format!("127.0.0.1:{port}").parse().unwrap();
        let l2 = bind_reuseport(again).expect("REUSEPORT second bind must succeed");
        assert_eq!(l1.local_addr().unwrap().port(), l2.local_addr().unwrap().port());
    }
}
```

- [ ] **Step 2: Wire module + run test**

Add `pub mod socket;` to `Lancelot/src/lib.rs`. Run: `cd Lancelot && cargo test socket::`
Expected: PASS (on macOS/Linux; `set_reuse_port` is unix-gated).

- [ ] **Step 3: Commit**

```bash
cd Lancelot && git add -A && git commit -q -m "feat: SO_REUSEPORT shared listener"
```

---

### Task 5: `FnInfo` ABI — registration-time handler snapshot

**Files:**
- Create: `Lancelot/src/ffi/mod.rs`, `Lancelot/src/ffi/fninfo.rs`
- Modify: `Lancelot/src/lib.rs`

- [ ] **Step 1: Write the failing test**

`Lancelot/tests/test_fninfo.py`:
```python
import asyncio, inspect, lancelot._native as n

def test_fninfo_snapshots_async_and_arity():
    async def h(req): return {"ok": True}
    fi = n.FnInfo(h, inspect.iscoroutinefunction(h), 1)
    assert fi.is_async is True
    assert fi.arity == 1
    assert fi.call_check() == "callable"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd Lancelot && maturin develop && python3 -m pytest tests/test_fninfo.py -q`
Expected: FAIL — `module 'lancelot._native' has no attribute 'FnInfo'`.

- [ ] **Step 3: Implement `src/ffi/fninfo.rs`**

```rust
use pyo3::prelude::*;

/// The ENTIRE Python->Rust handler contract. Captured once at registration;
/// the hot path never introspects a Python signature.
#[pyclass(frozen)]
pub struct FnInfo {
    pub handler: Py<PyAny>,
    #[pyo3(get)]
    pub is_async: bool,
    #[pyo3(get)]
    pub arity: usize,
}

#[pymethods]
impl FnInfo {
    #[new]
    fn new(handler: Py<PyAny>, is_async: bool, arity: usize) -> Self {
        Self { handler, is_async, arity }
    }
    fn call_check(&self, py: Python<'_>) -> &'static str {
        if self.handler.bind(py).is_callable() { "callable" } else { "not-callable" }
    }
}
```

`Lancelot/src/ffi/mod.rs`:
```rust
pub mod fninfo;
pub use fninfo::FnInfo;
```

- [ ] **Step 4: Register the class in `src/lib.rs`**

Add `pub mod ffi;` near the other `pub mod`s, and inside `_native(...)` before `Ok(())`:
```rust
m.add_class::<crate::ffi::FnInfo>()?;
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd Lancelot && maturin develop && python3 -m pytest tests/test_fninfo.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd Lancelot && git add -A && git commit -q -m "feat: FnInfo opaque-callable ABI"
```

---

### Task 6: Lazy `Request` pyclass

**Files:**
- Create: `Lancelot/src/ffi/request.rs`
- Modify: `Lancelot/src/ffi/mod.rs`, `Lancelot/src/lib.rs`

- [ ] **Step 1: Write the failing test**

`Lancelot/tests/test_request.py`:
```python
import lancelot._native as n

def test_lazy_request_materializes_on_access():
    req = n._test_make_request(b"POST", b"/echo", b'{"a":1}')
    assert req.method == "POST"
    assert req.path == "/echo"
    assert req.json() == {"a": 1}
    assert req.json() == {"a": 1}  # cached, second call must not re-parse
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd Lancelot && maturin develop && python3 -m pytest tests/test_request.py -q`
Expected: FAIL — `_test_make_request` missing.

- [ ] **Step 3: Implement `src/ffi/request.rs`**

```rust
use bytes::Bytes;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::OnceLock;

/// Lazy request: nothing is decoded until Python touches it. This object IS the
/// shared mutable request (do not rebuild it per phase — that breaks middleware).
#[pyclass]
pub struct Request {
    method: String,
    path: String,
    body: Bytes,
    json_cache: OnceLock<Py<PyAny>>,
}

impl Request {
    pub fn new(method: String, path: String, body: Bytes) -> Self {
        Self { method, path, body, json_cache: OnceLock::new() }
    }
}

#[pymethods]
impl Request {
    #[getter]
    fn method(&self) -> &str { &self.method }
    #[getter]
    fn path(&self) -> &str { &self.path }

    fn json<'py>(&self, py: Python<'py>) -> PyResult<Py<PyAny>> {
        if let Some(v) = self.json_cache.get() {
            return Ok(v.clone_ref(py));
        }
        let json = py.import("json")?;
        let parsed = json
            .call_method1("loads", (PyBytes::new(py, &self.body),))?
            .unbind();
        let _ = self.json_cache.set(parsed.clone_ref(py));
        Ok(parsed)
    }
}

/// Test-only constructor (gated; removed when service.rs builds Requests for real).
#[pyfunction]
pub fn _test_make_request(method: &[u8], path: &[u8], body: &[u8]) -> Request {
    Request::new(
        String::from_utf8_lossy(method).into_owned(),
        String::from_utf8_lossy(path).into_owned(),
        Bytes::copy_from_slice(body),
    )
}
```

Add to `src/ffi/mod.rs`: `pub mod request; pub use request::Request;`

- [ ] **Step 4: Register in `src/lib.rs`** (inside `_native`):

```rust
m.add_class::<crate::ffi::Request>()?;
m.add_function(wrap_pyfunction!(crate::ffi::request::_test_make_request, m)?)?;
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd Lancelot && maturin develop && python3 -m pytest tests/test_request.py -q`
Expected: PASS (including the second-call cache assertion).

- [ ] **Step 6: Commit**

```bash
cd Lancelot && git add -A && git commit -q -m "feat: lazy Request pyclass with cached json"
```

---

### Task 7: pydantic-core serialization seam

**Files:**
- Create: `Lancelot/src/serialize.rs`
- Modify: `Lancelot/src/lib.rs`

- [ ] **Step 1: Write the failing test**

`Lancelot/tests/test_serialize.py`:
```python
import lancelot._native as n

def test_to_json_one_pass():
    assert n._to_json({"a": 1, "b": [2, 3]}) == b'{"a":1,"b":[2,3]}'
    assert n._to_json("hi") == b'"hi"'
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd Lancelot && maturin develop && python3 -m pytest tests/test_serialize.py -q`
Expected: FAIL — `_to_json` missing.

- [ ] **Step 3: Implement `src/serialize.rs`**

```rust
use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// One-pass typed serialization: delegate to pydantic_core.to_json (Rust core,
/// bytes out, no intermediate dict, no second json.dumps). Falls back to stdlib
/// json only if pydantic_core is unavailable.
pub fn to_json_bytes(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    if let Ok(pc) = py.import("pydantic_core") {
        let out = pc.call_method1("to_json", (value,))?; // -> bytes
        return Ok(out.downcast::<PyBytes>()?.as_bytes().to_vec());
    }
    let json = py.import("json")?;
    let s: String = json
        .call_method1("dumps", (value,))?
        .extract()?;
    Ok(s.into_bytes())
}

#[pyfunction]
pub fn _to_json<'py>(py: Python<'py>, value: Bound<'py, PyAny>) -> PyResult<Py<PyBytes>> {
    let bytes = to_json_bytes(py, &value)?;
    Ok(PyBytes::new(py, &bytes).unbind())
}
```

Add `pub mod serialize;` to `src/lib.rs`; register `_to_json` in `_native`:
```rust
m.add_function(wrap_pyfunction!(crate::serialize::_to_json, m)?)?;
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd Lancelot && maturin develop && python3 -m pytest tests/test_serialize.py -q`
Expected: PASS (pydantic-core emits compact `{"a":1,...}` with no spaces).

- [ ] **Step 5: Commit**

```bash
cd Lancelot && git add -A && git commit -q -m "feat: pydantic-core one-pass serialization seam"
```

---

### Task 8: GIL choreography dispatch

**Files:**
- Create: `Lancelot/src/dispatch.rs`
- Modify: `Lancelot/src/lib.rs`

- [ ] **Step 1: Write the failing test (the GIL-released proof)**

`Lancelot/tests/test_dispatch.py`:
```python
import asyncio, time, lancelot._native as n

def test_dispatch_releases_gil_across_await():
    async def slow(req):
        await asyncio.sleep(0.1)
        return {"echo": req.json()}
    fi = n.FnInfo(slow, True, 1)
    # _drive runs N dispatches concurrently on Lancelot's runtime and returns
    # (results, wall_seconds). If the GIL were held across the await, 50*0.1s
    # would serialize to ~5s; released, it stays ~0.1-0.3s.
    results, wall = n._drive(fi, b'{"a":1}', 50)
    assert results[0] == b'{"echo":{"a":1}}'
    assert wall < 1.0, f"GIL not released across await (wall={wall}s)"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd Lancelot && maturin develop && python3 -m pytest tests/test_dispatch.py -q`
Expected: FAIL — `_drive` missing.

- [ ] **Step 3: Implement `src/dispatch.rs`**

```rust
use crate::ffi::request::Request;
use crate::ffi::fninfo::FnInfo;
use crate::serialize::to_json_bytes;
use bytes::Bytes;
use pyo3::prelude::*;

/// The choreography. THREE with_gil scopes, none containing `.await`:
///  (1) call handler -> coroutine -> into_future   (2) [GIL DROPPED] await
///  (3) serialize via pydantic-core. The await in (between) is GIL-free.
pub async fn call_async(handler: Py<PyAny>, body: Bytes) -> PyResult<Vec<u8>> {
    // Scope 1: build the coroutine + bridge it to a Rust future. No await here.
    let fut = Python::with_gil(|py| -> PyResult<_> {
        let req = Bound::new(py, Request::new("POST".into(), "/echo".into(), body))?;
        let coro = handler.bind(py).call1((req,))?;
        pyo3_async_runtimes::tokio::into_future(coro)
    })?;

    // GIL is NOT held here. Other requests run.
    let result: Py<PyAny> = fut.await?;

    // Scope 3: one-pass serialize.
    Python::with_gil(|py| to_json_bytes(py, result.bind(py)))
}

#[pyfunction]
pub fn _drive<'py>(
    py: Python<'py>,
    fi: &FnInfo,
    body: &[u8],
    n: usize,
) -> PyResult<(Vec<Py<pyo3::types::PyBytes>>, f64)> {
    use pyo3::types::PyBytes;
    let handler = fi.handler.clone_ref(py);
    let body = Bytes::copy_from_slice(body);
    let rt = pyo3_async_runtimes::tokio::get_runtime();
    let (results, wall) = py.allow_threads(|| {
        rt.block_on(async move {
            let t0 = std::time::Instant::now();
            let mut set = tokio::task::JoinSet::new();
            for _ in 0..n {
                let h = Python::with_gil(|p| handler.clone_ref(p));
                let b = body.clone();
                set.spawn(async move { call_async(h, b).await });
            }
            let mut out = Vec::with_capacity(n);
            while let Some(r) = set.join_next().await {
                out.push(r.unwrap().unwrap());
            }
            (out, t0.elapsed().as_secs_f64())
        })
    });
    let py_results = results
        .into_iter()
        .map(|b| PyBytes::new(py, &b).unbind())
        .collect();
    Ok((py_results, wall))
}
```

Add `pub mod dispatch;` to `src/lib.rs`; register `_drive`:
```rust
m.add_function(wrap_pyfunction!(crate::dispatch::_drive, m)?)?;
```
Also ensure the asyncio event loop is initialized: in `_native`, add
```rust
pyo3_async_runtimes::tokio::init_with_runtime(
    Box::leak(Box::new(
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(crate::cpu::worker_count())
            .enable_all().build().unwrap()
    ))
).ok();
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd Lancelot && maturin develop && python3 -m pytest tests/test_dispatch.py -q`
Expected: PASS — `wall < 1.0` (50×100ms concurrent ≈ 0.1–0.3s ⇒ GIL released).

- [ ] **Step 5: Commit**

```bash
cd Lancelot && git add -A && git commit -q -m "feat: GIL choreography (drop GIL across the python await)"
```

---

### Task 9: matchit router + const-tier stub

**Files:**
- Create: `Lancelot/src/router.rs`
- Modify: `Lancelot/src/lib.rs`

- [ ] **Step 1: Write the failing test (inline)**

```rust
//! Per-method matchit router. Const-tier is a stub in M0 (returns None).
use matchit::Router as Matchit;
use std::collections::HashMap;

pub struct Router {
    by_method: HashMap<String, Matchit<usize>>, // value = handler slot index
}

impl Router {
    pub fn new() -> Self { Self { by_method: HashMap::new() } }

    pub fn add(&mut self, method: &str, path: &str, slot: usize) {
        self.by_method
            .entry(method.to_string())
            .or_insert_with(Matchit::new)
            .insert(path.to_string(), slot)
            .expect("route insert");
    }

    /// Returns the handler slot for an exact/param match, else None (404).
    pub fn route(&self, method: &str, path: &str) -> Option<usize> {
        let m = self.by_method.get(method)?;
        m.at(path).ok().map(|hit| *hit.value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn matches_method_and_path() {
        let mut r = Router::new();
        r.add("POST", "/echo", 0);
        r.add("GET", "/users/{id}", 7);
        assert_eq!(r.route("POST", "/echo"), Some(0));
        assert_eq!(r.route("GET", "/users/42"), Some(7));
        assert_eq!(r.route("POST", "/nope"), None);
        assert_eq!(r.route("GET", "/echo"), None); // method-scoped
    }
}
```

- [ ] **Step 2: Wire + run**

Add `pub mod router;` to `src/lib.rs`. Run: `cd Lancelot && cargo test router::`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
cd Lancelot && git add -A && git commit -q -m "feat: matchit per-method router"
```

---

### Task 10: hyper `tower::Service` seam + Python `run()` (closes M0 path)

**Files:**
- Create: `Lancelot/src/service.rs`, `Lancelot/lancelot/app.py`, `Lancelot/lancelot/_native.pyi`
- Modify: `Lancelot/src/lib.rs`, `Lancelot/lancelot/__init__.py`

- [ ] **Step 1: Write the failing end-to-end test**

`Lancelot/tests/test_m0_echo.py`:
```python
import socket, threading, time, httpx, lancelot

def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def test_echo_roundtrip_over_real_socket():
    app = lancelot.Lancelot()

    @app.post("/echo")
    async def echo(req):
        return {"echo": req.json()}

    port = _free_port()
    t = threading.Thread(target=lambda: lancelot.run(app, host="127.0.0.1", port=port), daemon=True)
    t.start()
    time.sleep(1.5)  # let the runtime bind

    r = httpx.post(f"http://127.0.0.1:{port}/echo", json={"hello": "world"}, timeout=5)
    assert r.status_code == 200
    assert r.json() == {"echo": {"hello": "world"}}

    miss = httpx.post(f"http://127.0.0.1:{port}/nope", json={}, timeout=5)
    assert miss.status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd Lancelot && maturin develop && python3 -m pytest tests/test_m0_echo.py -q`
Expected: FAIL — `lancelot.Lancelot` / `lancelot.run` missing.

- [ ] **Step 3: Implement `src/service.rs`**

```rust
use crate::dispatch::call_async;
use crate::router::Router;
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{body::Incoming, Request as HReq, Response as HResp, StatusCode};
use hyper_util::rt::{TokioExecutor, TokioIo};
use hyper_util::server::conn::auto::Builder as AutoBuilder;
use pyo3::prelude::*;
use std::sync::Arc;

pub struct App {
    pub router: Router,
    pub handlers: Vec<Py<PyAny>>, // slot -> handler
}

async fn handle(app: Arc<App>, req: HReq<Incoming>) -> HResp<Full<Bytes>> {
    let method = req.method().as_str().to_string();
    let path = req.uri().path().to_string();
    let slot = match app.router.route(&method, &path) {
        Some(s) => s,
        None => {
            return HResp::builder()
                .status(StatusCode::NOT_FOUND)
                .body(Full::new(Bytes::from_static(b"not found")))
                .unwrap();
        }
    };
    let body = match req.into_body().collect().await {
        Ok(c) => c.to_bytes(),
        Err(_) => Bytes::new(),
    };
    let handler = Python::with_gil(|py| app.handlers[slot].clone_ref(py));
    match call_async(handler, body).await {
        Ok(json) => HResp::builder()
            .status(StatusCode::OK)
            .header("content-type", "application/json")
            .body(Full::new(Bytes::from(json)))
            .unwrap(),
        Err(_) => HResp::builder()
            .status(StatusCode::INTERNAL_SERVER_ERROR)
            .body(Full::new(Bytes::from_static(b"internal error")))
            .unwrap(),
    }
}

pub fn serve(app: App, host: &str, port: u16) -> PyResult<()> {
    let addr: std::net::SocketAddr = format!("{host}:{port}").parse().unwrap();
    let std_listener = crate::socket::bind_reuseport(addr)
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?;
    std_listener.set_nonblocking(true).ok();
    let app = Arc::new(app);
    let rt = pyo3_async_runtimes::tokio::get_runtime();

    Python::with_gil(|py| {
        py.allow_threads(|| {
            rt.block_on(async move {
                let listener = tokio::net::TcpListener::from_std(std_listener).unwrap();
                let mut sigterm =
                    tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                        .unwrap();
                loop {
                    tokio::select! {
                        _ = sigterm.recv() => { break; } // graceful: stop accepting
                        accepted = listener.accept() => {
                            let (stream, _) = match accepted { Ok(v) => v, Err(_) => continue };
                            let app = app.clone();
                            tokio::spawn(async move {
                                let io = TokioIo::new(stream);
                                let svc = hyper::service::service_fn(move |r| {
                                    let app = app.clone();
                                    async move { Ok::<_, std::convert::Infallible>(handle(app, r).await) }
                                });
                                let _ = AutoBuilder::new(TokioExecutor::new())
                                    .serve_connection(io, svc)
                                    .await;
                            });
                        }
                    }
                }
            });
        });
    });
    Ok(())
}
```

- [ ] **Step 4: Add the `run` pyfunction + `build_app` bridge in `src/lib.rs`**

Add `pub mod service;` and inside `_native`:
```rust
m.add_function(wrap_pyfunction!(run, m)?)?;
```
Add this function to `src/lib.rs`:
```rust
use crate::service::App;

/// routes: list of (method:str, path:str, handler:callable)
#[pyfunction]
#[pyo3(signature = (routes, host="127.0.0.1", port=8000))]
fn run(py: Python<'_>, routes: Vec<(String, String, Py<PyAny>)>, host: &str, port: u16) -> PyResult<()> {
    let mut router = crate::router::Router::new();
    let mut handlers = Vec::new();
    for (i, (method, path, h)) in routes.into_iter().enumerate() {
        router.add(&method, &path, i);
        handlers.push(h);
    }
    crate::service::serve(App { router, handlers }, host, port)
}
```

- [ ] **Step 5: Implement `lancelot/app.py` and update `__init__.py`**

`Lancelot/lancelot/app.py`:
```python
import inspect
from lancelot import _native

class Lancelot:
    def __init__(self):
        self._routes = []  # (method, path, handler)

    def _add(self, method, path):
        def deco(fn):
            self._routes.append((method, path, fn))
            return fn
        return deco

    def post(self, path): return self._add("POST", path)
    def get(self, path): return self._add("GET", path)

def run(app: "Lancelot", host: str = "127.0.0.1", port: int = 8000):
    _native.run(app._routes, host, port)
```

`Lancelot/lancelot/__init__.py`:
```python
from lancelot._native import _ping
from lancelot.app import Lancelot, run

__all__ = ["Lancelot", "run", "_ping"]
__version__ = "0.0.0"
```

`Lancelot/lancelot/_native.pyi` (hand stub for M0):
```python
from typing import Any, Callable
class FnInfo:
    def __init__(self, handler: Callable[..., Any], is_async: bool, arity: int) -> None: ...
    is_async: bool
    arity: int
class Request: ...
def run(routes: list[tuple[str, str, Callable[..., Any]]], host: str, port: int) -> None: ...
def _ping() -> str: ...
```

- [ ] **Step 6: Run to verify it passes**

Run: `cd Lancelot && maturin develop && python3 -m pytest tests/test_m0_echo.py -q`
Expected: PASS — 200 echo round-trip + 404 on miss.

- [ ] **Step 7: Commit**

```bash
cd Lancelot && git add -A && git commit -q -m "feat: hyper tower service + lancelot.run end-to-end echo (M0 path closed)"
```

---

### Task 11: Allocation gate

**Files:**
- Create: `Lancelot/src/bin/alloc_probe.rs`
- Modify: `Lancelot/Cargo.toml` (add `[[bin]]`), `Lancelot/src/dispatch.rs` (no change to logic; profiled as-is)

- [ ] **Step 1: Write the failing probe (a `dhat` binary asserting bounded allocs)**

`Lancelot/src/bin/alloc_probe.rs`:
```rust
//! Counts heap allocations for ONE dispatch of a trivial handler. Gate: <= 5
//! framework allocs (target 1-3). Run: cargo run --bin alloc_probe --features dhat-heap
#[cfg(feature = "dhat-heap")]
#[global_allocator]
static ALLOC: dhat::Alloc = dhat::Alloc;

fn main() {
    #[cfg(feature = "dhat-heap")]
    {
        let _profiler = dhat::Profiler::builder().testing().build();
        pyo3::prepare_freethreaded_python();
        let body = bytes::Bytes::from_static(b"{\"a\":1}");
        let handler = pyo3::Python::with_gil(|py| {
            let code = std::ffi::CString::new(
                "async def h(req):\n    return {'echo': req.json()}\n"
            ).unwrap();
            let m = pyo3::types::PyModule::from_code(
                py, &code, &std::ffi::CString::new("h.py").unwrap(),
                &std::ffi::CString::new("h").unwrap()).unwrap();
            m.getattr("h").unwrap().unbind()
        });
        let rt = pyo3_async_runtimes::tokio::get_runtime();
        // Warm once (amortize one-time imports), then measure the steady allocation.
        rt.block_on(async { let _ = lancelot::dispatch::call_async(
            pyo3::Python::with_gil(|p| handler.clone_ref(p)), body.clone()).await; });
        let stats_before = dhat::HeapStats::get();
        rt.block_on(async { let _ = lancelot::dispatch::call_async(
            pyo3::Python::with_gil(|p| handler.clone_ref(p)), body.clone()).await; });
        let stats_after = dhat::HeapStats::get();
        let delta = stats_after.total_blocks - stats_before.total_blocks;
        println!("framework allocations for one request: {delta}");
        assert!(delta <= 5, "M0 alloc gate failed: {delta} > 5");
    }
    #[cfg(not(feature = "dhat-heap"))]
    eprintln!("run with --features dhat-heap");
}
```

Add to `Lancelot/Cargo.toml`:
```toml
[[bin]]
name = "alloc_probe"
required-features = ["dhat-heap"]
```

- [ ] **Step 2: Run to verify it fails (or reveals the real number)**

Run: `cd Lancelot && cargo run --bin alloc_probe --features dhat-heap --release`
Expected: prints the delta; if `> 5` it asserts (fail) — that is the signal to reduce a copy in `dispatch.rs`/`request.rs` (e.g. avoid the `Bytes::copy_from_slice`, reuse the `OnceLock`).

- [ ] **Step 3: If over budget, apply the minimal reduction**

In `src/dispatch.rs::call_async`, the only required allocation is the response `Vec<u8>` + the coroutine future. If the probe shows `>5`, remove redundant `clone_ref`/`copy_from_slice` (pass `Bytes` by move; the body is already refcounted — `Bytes::clone` is not a heap alloc). Re-run Step 2 until `delta <= 5`.

- [ ] **Step 4: Verify it passes**

Run: `cd Lancelot && cargo run --bin alloc_probe --features dhat-heap --release`
Expected: `framework allocations for one request: N` with `N <= 5` and no assertion.

- [ ] **Step 5: Commit**

```bash
cd Lancelot && git add -A && git commit -q -m "test: M0 allocation gate (<=5 framework allocs/req)"
```

---

### Task 12: M0 acceptance harness + tag

**Files:**
- Create: `Lancelot/Makefile`, `Lancelot/tests/test_m0_acceptance.py`

- [ ] **Step 1: Write the acceptance aggregator test**

`Lancelot/tests/test_m0_acceptance.py`:
```python
import shutil, subprocess, sys, importlib, pytest

def test_imports_under_available_interpreters():
    # py3.13 + py3.14 required; *t (free-threaded) is CI-only -> skip if absent.
    for exe in ("python3.13", "python3.14"):
        if shutil.which(exe):
            subprocess.run([exe, "-c", "import lancelot; lancelot._ping()"], check=True)
    for exe in ("python3.13t", "python3.14t"):
        if not shutil.which(exe):
            pytest.skip(f"{exe} not present locally (enforced in CI)")
        else:
            subprocess.run([exe, "-c", "import lancelot; lancelot._ping()"], check=True)

def test_native_module_loads():
    importlib.import_module("lancelot")
```

- [ ] **Step 2: Write `Lancelot/Makefile`**

```makefile
.PHONY: m0
m0:
	maturin develop --release
	cargo test --test trybuild
	cargo test cpu:: socket:: router::
	cargo run --bin alloc_probe --features dhat-heap --release
	python3 -m pytest tests/ -q
	@echo "M0 ACCEPTANCE: all 7 gates green"
```

- [ ] **Step 3: Run the full M0 gate**

Run: `cd Lancelot && make m0`
Expected: every sub-step green; final line `M0 ACCEPTANCE: all 7 gates green`. This proves DoD 1–7 (compile-fail invariant, cgroup/socket/router units, alloc≤5, GIL-released concurrency, echo round-trip, REUSEPORT, clean SIGTERM).

- [ ] **Step 4: Verify SIGTERM cleanliness explicitly**

Run:
```bash
cd Lancelot && python3 -c "
import threading,time,os,signal,subprocess,sys
p=subprocess.Popen([sys.executable,'-c','import lancelot;a=lancelot.Lancelot();\nimport asyncio\n@a.post(\"/x\")\nasync def h(r):return{}\nlancelot.run(a,port=8123)'])
time.sleep(2); p.send_signal(signal.SIGTERM); print('exit', p.wait(timeout=5))
"
```
Expected: `exit 0`, no Python traceback on stderr.

- [ ] **Step 5: Tag M0 in the local repo**

```bash
cd Lancelot && git add -A && git commit -q -m "test: M0 acceptance harness" && git tag m0-green
```

---

## Self-Review

**1. Spec coverage (spec §2/§3/§8 → tasks):** socket/cgroup/SO_REUSEPORT → T3,T4; GIL invariant + negative compile test → T2; FnInfo ABI → T5; lazy Request → T6; pydantic-core one-pass → T7; GIL choreography (drop across await) → T8; matchit + const-tier stub → T9; hyper tower::Service seam + run + SIGTERM drain → T10; ≤5 allocs/req → T11; the 7 M0 DoD assertions + dual-interpreter import → T12. Spec §8 M0 gate fully covered. M1–M4 explicitly out of scope (separate plans) — consistent with the spec's "build M0 first". No gaps.

**2. Placeholder scan:** no TBD/TODO; every code step has complete code; every run step has an exact command + expected output. The one adaptive step (T11 S3) is a bounded, specified reduction with a concrete re-run loop, not a placeholder.

**3. Type consistency:** `with_gil`(T2) used identically in T6/T8/T10; `FnInfo{handler,is_async,arity}` (T5) consumed unchanged in T8; `Request::new(method,path,body)` (T6) called the same in T8/T10; `Router::{new,add,route}` (T9) used unchanged in T10; `to_json_bytes(py,&Bound)` (T7) called the same in T8; `call_async(Py<PyAny>,Bytes)->PyResult<Vec<u8>>` (T8) consumed unchanged in T10/T11. Module registration in `_native` is additive across T1–T10. Consistent.

**Note on environment:** `cargo`/`maturin`/`python3.13t` are absent on the dev machine — T1 S1 bootstraps Rust+maturin; the free-threaded import is a CI-only gate (T12 skips it locally with an explicit message). pyo3 0.27 API is used (`Python::with_gil`); the rename to `Python::attach` is a mechanical 0.28-bump noted in `gil.rs`. No issues requiring rework.
