"""M0 DoD #7: `lancelot.run` serves; SIGTERM exits cleanly (no traceback,
exit 0). tokio's SIGTERM handler breaks the accept loop → run_until_complete
returns → run() returns → the process exits normally."""

import signal
import socket
import subprocess
import sys
import time


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


_PROG = """
import lancelot
app = lancelot.Lancelot()

@app.post("/x")
async def h(req):
    return {}

lancelot.run(app, host="127.0.0.1", port=%d)
"""


def test_sigterm_exits_cleanly():
    port = _free_port()
    p = subprocess.Popen(
        [sys.executable, "-c", _PROG % port],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(2.0)  # let it bind
    assert p.poll() is None, "server exited before SIGTERM"
    p.send_signal(signal.SIGTERM)
    try:
        _, err = p.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        raise AssertionError("SIGTERM did not exit within 5s (no graceful drain)")
    assert p.returncode == 0, f"exit {p.returncode}, stderr:\n{err}"
    assert "Traceback" not in err, f"traceback on SIGTERM:\n{err}"
