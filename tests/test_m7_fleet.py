"""M7 T2.3 — /fleet/status + region tagging (Conductor pattern).

Per-worker snapshot endpoint that aggregators (HAProxy/Lua, nginx,
custom) GET to roll up multi-region / multi-process fleet state.
"""
import lancelot
from lancelot.fleet import fleet_info, region, reset_for_tests, worker_id
from lancelot.testclient import TestClient


def test_t2_3_region_default():
    reset_for_tests()
    assert region() == "default"


def test_t2_3_region_from_env(monkeypatch):
    monkeypatch.setenv("LANCELOT_REGION", "eu-west-1")
    reset_for_tests()
    assert region() == "eu-west-1"


def test_t2_3_worker_id_default_is_pid():
    """Worker id defaults to the process PID — operators set it
    explicitly when running multiple workers."""
    import os
    reset_for_tests()
    assert worker_id() == str(os.getpid())


def test_t2_3_worker_id_from_env(monkeypatch):
    monkeypatch.setenv("LANCELOT_WORKER_ID", "worker-7")
    reset_for_tests()
    assert worker_id() == "worker-7"


def test_t2_3_fleet_info_shape():
    """fleet_info() returns the expected dict shape."""
    reset_for_tests()
    app = lancelot.App()
    info = fleet_info(app)
    assert set(info.keys()) == {
        "region", "worker_id", "status", "in_flight",
        "uptime_s", "lancelot_version"}
    assert info["status"] == "ok"
    assert info["in_flight"] == 0
    assert info["uptime_s"] >= 0


def test_t2_3_fleet_status_endpoint_returns_json(monkeypatch):
    monkeypatch.setenv("LANCELOT_REGION", "us-east-1")
    monkeypatch.setenv("LANCELOT_WORKER_ID", "w1")
    reset_for_tests()
    app = lancelot.App()
    with TestClient(app) as client:
        r = client.get("/fleet/status")
        assert r.status_code == 200
        body = r.json()
        assert body["region"] == "us-east-1"
        assert body["worker_id"] == "w1"
        assert body["status"] == "ok"


def test_t2_3_fleet_status_reflects_draining():
    reset_for_tests()
    app = lancelot.App()
    app.initiate_shutdown()
    info = fleet_info(app)
    assert info["status"] == "draining"
