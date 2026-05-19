"""M1-P5 LEDGER (quarantined, excluded from the ratchet) — the
genuinely-deferred blocking-pool / background tail. M1-P5 ships the
behavior on the Python ASGI seam (the path the conformance/byte oracle
actually exercises): a bounded stdlib blocking pool offloads sync
handlers + sync deps, overload → byte 503 Retry-After, BackgroundTasks
byte/timing-faithful to fastapi 0.136.1, and a detached background.spawn
pool. RED here = the parts NOT in M1-P5 scope:

  1. The Rust 3-variant Empty/Mono/Pool runner (src/blocking/runner.rs)
     + crossbeam BOUNDED(10_000) + py.detach()-on-recv + adaptive
     single-flight spawn + idle-timeout death, AND src/background/
     pool.rs — these belong to the spec §3-69 Rust-pipeline unification
     (TestClient drives the Python ASGI seam today; the Python pool is
     the realization the byte oracle reaches). DO NOT silently pass.
  2. /metrics queue-depth / in-flight / rejected counters — that is
     U35 (M3 observability), not M1.
  3. Graceful-shutdown drain of in-flight blocking + background work on
     lifespan shutdown / SIGTERM (U34 / U7, M3).
  4. Callable-INSTANCE handlers/deps (an object whose `__call__` is
     `def`/`async def`). FastAPI's get_typed_signature / is_coroutine_
     callable resolve the signature + coroutine-ness via
     `type(obj).__call__`; Lancelot's compile_dependant (M1-P1)
     `typing.get_type_hints(fn)` raises TypeError at REGISTRATION for an
     instance — verified failing on m1-p4-green too, so this is a
     PRE-EXISTING DI-surface gap, NOT an M1-P5 regression (M1-P5 only
     changed the function-handler offload path; the M1-P5 review caught
     this via the byte oracle). Belongs with the DI/U15-U18 surface
     work; ledgered honestly rather than silently narrowed.
  5. Post-send background-task exceptions: byte/observable parity holds
     (wire is 200, the yield-dep teardown still sees the exception, same
     as FastAPI), but Lancelot's A5 seam (`asgi.py` `except Exception`)
     silently absorbs the post-send failure whereas FastAPI lets it
     propagate so the server LOGS it. The only divergence is server-side
     error visibility/logging — that is U35 (M3 JSON-logs/observability),
     not M1; recorded so it is not hidden.
Graduate into conformance/vendor/ as M3 + the §3-69 unification land."""
import pytest


def test_rust_blocking_runner_and_bg_pool_unification_S369():
    pytest.fail("spec §3-69: Rust src/blocking/runner.rs + "
                "src/background/pool.rs wired into the Rust hyper path "
                "(M1-P5 ships the byte-faithful Python-ASGI-seam pool)")


def test_pool_metrics_queue_depth_U35_M3():
    pytest.fail("U35/M3: blocking_pool_* + background queue depth in "
                "/metrics (M1-P5 enforces the bound; metrics are M3)")


def test_graceful_drain_on_shutdown_M3():
    pytest.fail("M3/U34: drain in-flight blocking+background work on "
                "lifespan shutdown / SIGTERM")


def test_callable_instance_handler_DI_surface_preexisting():
    pytest.fail("DI/U15-U18 (pre-existing, not M1-P5): compile_dependant "
                "must resolve hints + coroutine-ness via type(obj).__call__ "
                "for callable-instance handlers/deps (FastAPI parity)")
