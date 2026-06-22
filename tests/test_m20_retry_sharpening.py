"""M20 retry-sharpening on TransportPool (byte-path mine FINDINGS T1.9/T3.3/T3.5).

- T3.3: honor Azure OpenAI's `x-ms-retry-after-ms` header.
- T3.5: honor the provider `x-should-retry` hint over the status heuristic.
- T1.9: equal-jitter backoff so a fanout of N retriers doesn't re-fire in
  lockstep at a rate-limited LLM (the thundering-herd the @app.batch fanout hits).

All deterministic: the helpers are pure (static / arithmetic), tested directly.
"""
import types

from lancelot.llm import TransportPool


def _resp(status=200, **headers):
    return types.SimpleNamespace(status_code=status, headers=dict(headers))


# ---------- T3.3: Retry-After header parsing ----------

def test_retry_after_azure_ms_header():
    assert TransportPool._retry_after_seconds(
        _resp(**{"x-ms-retry-after-ms": "2000"})) == 2.0


def test_retry_after_standard_and_ms_still_work():
    assert TransportPool._retry_after_seconds(_resp(**{"retry-after": "3"})) == 3.0
    assert TransportPool._retry_after_seconds(
        _resp(**{"retry-after-ms": "1500"})) == 1.5


def test_retry_after_absent_is_none():
    assert TransportPool._retry_after_seconds(_resp()) is None


def test_retry_after_seconds_takes_precedence():
    r = _resp(**{"retry-after": "5", "x-ms-retry-after-ms": "1000"})
    assert TransportPool._retry_after_seconds(r) == 5.0


def test_retry_after_garbage_is_none():
    assert TransportPool._retry_after_seconds(
        _resp(**{"x-ms-retry-after-ms": "soon"})) is None


# ---------- T3.5: x-should-retry override ----------

def test_is_retryable_status_defaults_unchanged():
    for s in (429, 500, 502, 503, 504):
        assert TransportPool._is_retryable(_resp(s)) is True
    for s in (200, 201, 400, 404, 422):
        assert TransportPool._is_retryable(_resp(s)) is False


def test_x_should_retry_false_blocks_a_5xx():
    assert TransportPool._is_retryable(
        _resp(503, **{"x-should-retry": "false"})) is False


def test_x_should_retry_true_forces_a_4xx():
    assert TransportPool._is_retryable(
        _resp(400, **{"x-should-retry": "true"})) is True
    assert TransportPool._is_retryable(
        _resp(400, **{"x-should-retry": "TRUE"})) is True  # case-insensitive


# ---------- T1.9: equal-jitter backoff ----------

def test_backoff_wait_bounds_and_cap():
    pool = TransportPool()
    base, mx = pool.backoff_base, pool.backoff_max
    for attempt in range(7):
        cap = min(mx, base * (2 ** attempt))
        for _ in range(20):
            w = pool._backoff_wait(attempt)
            assert cap / 2.0 <= w <= cap


def test_backoff_wait_is_decorrelated_not_lockstep():
    pool = TransportPool()
    samples = [pool._backoff_wait(3) for _ in range(50)]
    assert len(set(samples)) > 5  # spread across the window, not one value
    cap = min(pool.backoff_max, pool.backoff_base * (2 ** 3))
    assert min(samples) >= cap / 2.0
    assert max(samples) <= cap
