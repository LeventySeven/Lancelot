import socket
import threading
import time

import httpx

import lancelot


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_echo_roundtrip_over_real_socket():
    app = lancelot.Lancelot()

    @app.post("/echo")
    async def echo(req):
        return {"echo": req.json()}

    port = _free_port()
    t = threading.Thread(
        target=lambda: lancelot.run(app, host="127.0.0.1", port=port),
        daemon=True,
    )
    t.start()
    time.sleep(1.5)  # let the runtime bind

    r = httpx.post(f"http://127.0.0.1:{port}/echo", json={"hello": "world"}, timeout=5)
    assert r.status_code == 200
    assert r.json() == {"echo": {"hello": "world"}}

    miss = httpx.post(f"http://127.0.0.1:{port}/nope", json={}, timeout=5)
    assert miss.status_code == 404
