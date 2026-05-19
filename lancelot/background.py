"""BackgroundTasks (FastAPI byte/timing parity — tasks run AFTER the
response is sent) + the detached background pool (WBS U33). Sync tasks
run on the M1-P5 blocking pool so they never pin the reactor; FastAPI
runs them via run_in_threadpool too. Stdlib-only (runtime dep =
pydantic)."""
import inspect


class BackgroundTasks:
    def __init__(self):
        self._tasks = []

    def add_task(self, func, /, *args, **kwargs):
        self._tasks.append((func, args, kwargs))

    async def __call__(self):
        from lancelot.pool import get_pool

        for func, args, kwargs in self._tasks:
            if inspect.iscoroutinefunction(func):
                await func(*args, **kwargs)
            else:
                await get_pool().offload(func, *args, **kwargs)
