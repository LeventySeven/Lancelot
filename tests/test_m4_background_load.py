"""M4 sweep — BackgroundTasks under REAL load (AI: post-response cost logging,
vector writes, audit). They run after the body is sent — async tasks on the
loop, sync tasks on a thread pool via `copy_context`. Under concurrency every
task must (a) actually RUN and (b) see ITS OWN request context (the B3
copy_context propagation), with zero cross-request bleed.
"""
import concurrent.futures as cf
import signal
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")

_SERVER = r"""
import sys
import uvicorn
import lancelot
from lancelot.background import BackgroundTasks
from lancelot.context import request_id

app = lancelot.App()
_seen = []  # (kind, rid_seen_in_task, rid_expected)

@app.get("/work")
async def work(bg: BackgroundTasks):
    rid = request_id.get()

    def sync_task(expected):      # runs on the bg thread pool (copy_context)
        _seen.append(("sync", request_id.get(), expected))

    async def async_task(expected):  # runs on the event loop
        _seen.append(("async", request_id.get(), expected))

    bg.add_task(sync_task, rid)
    bg.add_task(async_task, rid)
    return {"rid": rid}

@app.get("/seen")
async def seen():
    return {"n": len(_seen),
            "mismatches": [s for s in _seen if s[1] != s[2]]}

uvicorn.run(app.asgi(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="warning", access_log=False, loop="uvloop", http="httptools")
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def server():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-c", _SERVER, str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 12
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(proc.stderr.read().decode()[:500])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_background_tasks_run_with_correct_context_under_load(server):
    n = 30

    def hit(i):
        return httpx.get(f"{server}/work", headers={"X-Request-ID": f"r{i}"}).json()["rid"]

    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        rids = list(ex.map(hit, range(n)))

    # Each handler saw its own request-id.
    assert sorted(rids) == sorted(f"r{i}" for i in range(n)), rids

    # Background tasks run after the body is sent — poll until all 2n drained.
    deadline = time.time() + 10
    st = {"n": 0, "mismatches": []}
    while time.time() < deadline:
        st = httpx.get(f"{server}/seen").json()
        if st["n"] >= 2 * n:
            break
        time.sleep(0.05)

    assert st["n"] == 2 * n, f"only {st['n']}/{2 * n} background tasks ran"
    # Every task (sync thread-pool + async loop) saw ITS OWN request-id — no bleed.
    assert st["mismatches"] == [], f"context bled across requests: {st['mismatches'][:5]}"
