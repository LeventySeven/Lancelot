"""M6 T2.1 — self-tests for the soak driver's leak guards G3 + G4.

The guards live in benchmarks/m5/soak.py. They're not part of the lancelot
package (intentionally; soak.py is a bench-only utility) so we add the
benchmarks dir to sys.path and import directly.
"""
from __future__ import annotations

import csv
import io
import os
import sys

import pytest


# Make benchmarks/m5 importable.
_BENCH_M5 = os.path.join(os.path.dirname(__file__), "..", "benchmarks", "m5")
if _BENCH_M5 not in sys.path:
    sys.path.insert(0, _BENCH_M5)


@pytest.fixture
def soak():
    """Import the soak module lazily; pytest collects fail-fast on
    missing modules."""
    import soak  # type: ignore[import-not-found]
    return soak


def _make_writer():
    """A no-op CSV DictWriter (writes to /dev/null in-memory)."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=[
        "t_unix_start", "t_unix_end", "n_ok", "n_err", "succ_rate",
        "p50_ms", "p95_ms", "p99_ms", "rss_kb", "fd_count",
    ])
    w.writeheader()
    return w, buf


def _make_window(soak, *, n_ok=100, n_err=0, lats=(1.0,) * 100,
                 rss_kb=0.0, fd_count=0):
    """Build a WindowStats with the given samples + values."""
    w = soak.WindowStats(t_start=0.0, t_end=30.0)
    w.n_ok = n_ok
    w.n_err = n_err
    w.latencies_ms = list(lats)
    w.rss_kb = rss_kb
    w.fd_count = fd_count
    return w


def test_g1_succ_rate_fails_below_99(soak):
    writer, _ = _make_writer()
    w = _make_window(soak, n_ok=98, n_err=2, lats=(1.0,) * 98)
    ok = soak.emit_window(writer, w, baseline_p99_ms=None)
    assert ok is False  # 98/100 = 98% < 99% → G1 fires


def test_g2_p99_fails_above_2x_baseline(soak):
    writer, _ = _make_writer()
    # Baseline p99 = 10 ms; observed p99 = 25 ms → 2.5× → G2 fires
    w = _make_window(soak, n_ok=100, n_err=0,
                     lats=tuple([5.0] * 99) + (25.0,))
    ok = soak.emit_window(writer, w, baseline_p99_ms=10.0)
    assert ok is False


def test_g3_rss_slope_fires_on_synthetic_leak(soak):
    """Simulate a leak that grows RSS by 10%/h over a 4-hour window.
    G3 threshold is 5%/h → should fire."""
    writer, _ = _make_writer()
    w = _make_window(soak, lats=(1.0,) * 100)
    # 30-second window samples over 4 hours = 480 samples.
    # Start at 1,000,000 KB, grow ~10%/h → reaches ~1,400,000 KB at 4h.
    start_kb = 1_000_000.0
    rss_history = []
    n_samples = int(4 * 3600 / 30)
    for i in range(n_samples):
        t = i * 30.0
        # 10%/h = 10/3600 %/s = 1/360 fraction/s; over 4h → grows ~0.4×
        kb = start_kb * (1 + (i * 30.0) / 3600 * 0.10)
        rss_history.append((t, kb))
    ok = soak.emit_window(writer, w, baseline_p99_ms=None,
                          rss_history=rss_history, fd_history=None)
    assert ok is False


def test_g3_rss_slope_holds_on_flat_history(soak):
    """Flat RSS over 4 hours → G3 must not fire."""
    writer, _ = _make_writer()
    w = _make_window(soak, lats=(1.0,) * 100)
    n_samples = int(4 * 3600 / 30)
    # Add tiny noise (< 1%) to avoid degenerate zero-variance case.
    rss_history = [(i * 30.0, 1_000_000.0 + (i % 5) * 100) for i in range(n_samples)]
    ok = soak.emit_window(writer, w, baseline_p99_ms=None,
                          rss_history=rss_history, fd_history=None)
    assert ok is True


def test_g4_fd_monotonic_fires_on_synthetic_leak(soak):
    """Simulate an FD leak that grows monotonically over 1 hour from 100 to 200.
    G4 threshold: monotonic non-decreasing AND grew ≥ 20% (or 10 fds) → fires."""
    writer, _ = _make_writer()
    w = _make_window(soak, lats=(1.0,) * 100)
    n_samples = int(1 * 3600 / 30)  # 120 samples
    fd_history = []
    for i in range(n_samples):
        t = i * 30.0
        # Linear growth 100 → 200 over the hour.
        fd = 100 + i  # 100 at start, 220 at end (always >= prev)
        fd_history.append((t, fd))
    ok = soak.emit_window(writer, w, baseline_p99_ms=None,
                          rss_history=None, fd_history=fd_history)
    assert ok is False


def test_g4_fd_monotonic_holds_on_oscillating_history(soak):
    """FD count oscillates around a baseline → G4 must not fire."""
    writer, _ = _make_writer()
    w = _make_window(soak, lats=(1.0,) * 100)
    n_samples = int(1 * 3600 / 30)
    # Bounce between 100 and 105 — not monotonic.
    fd_history = [(i * 30.0, 100 + (i % 6)) for i in range(n_samples)]
    ok = soak.emit_window(writer, w, baseline_p99_ms=None,
                          rss_history=None, fd_history=fd_history)
    assert ok is True


def test_g3_g4_inactive_when_no_history(soak):
    """When the caller doesn't pass histories (local soak, no --remote-host),
    G3 + G4 must not fire."""
    writer, _ = _make_writer()
    w = _make_window(soak, lats=(1.0,) * 100, rss_kb=0, fd_count=0)
    ok = soak.emit_window(writer, w, baseline_p99_ms=None)
    assert ok is True


def test_linreg_slope_basics(soak):
    """Sanity check on the linear regression helper."""
    # y = 2x + 1 → slope = 2
    xs = [0.0, 1.0, 2.0, 3.0, 4.0]
    ys = [1.0, 3.0, 5.0, 7.0, 9.0]
    assert abs(soak._linreg_slope(xs, ys) - 2.0) < 1e-9
    # Constant → slope 0
    assert soak._linreg_slope([0.0, 1.0, 2.0], [5.0, 5.0, 5.0]) == 0.0
    # Single point → 0 (degenerate)
    assert soak._linreg_slope([1.0], [2.0]) == 0.0
