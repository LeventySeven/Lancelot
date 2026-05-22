"""M4 feature-steal (Bifrost) — TransportPool's streaming timeout is split from
the unary one. A long LLM token stream must NOT be severed by the unary read
deadline (a 30s unary timeout would kill any >30s generation). The stream path
disables the READ deadline (connect/write still bounded); runaway streams are
bounded by client-disconnect cancellation (C2) instead.
"""
import pytest

pytest.importorskip("httpx")

from lancelot.llm import TransportPool


def test_stream_timeout_disables_read_deadline_but_unary_stays_bounded():
    pool = TransportPool(timeout=7.0)
    try:
        # Stream path: NO read deadline (a steady token feed never read-times-out)
        # but connect/write/pool still bounded so a dead upstream is caught.
        assert pool._stream_timeout.read is None
        assert pool._stream_timeout.connect == 7.0
        assert pool._stream_timeout.write == 7.0
        # Unary path is UNCHANGED — still fully bounded at `timeout`.
        assert pool._client.timeout.read == 7.0
    finally:
        import asyncio
        asyncio.run(pool.aclose())
