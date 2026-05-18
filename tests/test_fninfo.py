import inspect

import lancelot._native as n


def test_fninfo_snapshots_async_and_arity():
    async def h(req):
        return {"ok": True}

    fi = n.FnInfo(h, inspect.iscoroutinefunction(h), 1)
    assert fi.is_async is True
    assert fi.arity == 1
    assert fi.call_check() == "callable"
