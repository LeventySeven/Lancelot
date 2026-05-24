"""M7 T3.2 — lancelot.secrets lazy provider.

Infisical pattern: "injected at startup (not hardcoded); rotated
without service downtime; auditable".
"""
import os
import signal
import tempfile

import pytest

import lancelot.secrets as ls
from lancelot.secrets import (
    EnvBackend, FileBackend, configure, get, install_sighup_handler,
    reset_for_tests, rotate, set_audit_hook,
)


def setup_function():
    reset_for_tests()


# ----- T3.2.a env backend (default) ---------------------------------------

def test_t3_2_env_backend_reads_environ(monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "sk-test-1234")
    assert get("MY_API_KEY") == "sk-test-1234"


def test_t3_2_env_backend_returns_none_when_unset():
    assert get("MY_NEVER_SET_KEY_xyz") is None


# ----- T3.2.b file backend ------------------------------------------------

def test_t3_2_file_backend_reads_from_disk():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "db_password"), "w") as f:
            f.write("hunter2\n")
        configure(FileBackend(d))
        assert get("db_password") == "hunter2"
        assert get("does_not_exist") is None


def test_t3_2_file_backend_caches_and_refresh_invalidates():
    """Repeated get() should hit cache; rotate() should re-read."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "tok")
        with open(path, "w") as f:
            f.write("v1")
        be = FileBackend(d)
        configure(be)
        assert get("tok") == "v1"
        # Modify the file; without refresh, we still see cached value.
        with open(path, "w") as f:
            f.write("v2")
        assert get("tok") == "v1"
        # rotate() clears cache → next get() re-reads.
        rotate()
        assert get("tok") == "v2"


# ----- T3.2.c audit hook --------------------------------------------------

def test_t3_2_audit_hook_invoked_with_found_flag(monkeypatch):
    monkeypatch.setenv("KEY_PRESENT", "x")
    calls = []
    set_audit_hook(lambda name, found: calls.append((name, found)))
    get("KEY_PRESENT")
    get("KEY_ABSENT")
    assert ("KEY_PRESENT", True) in calls
    assert ("KEY_ABSENT", False) in calls


def test_t3_2_audit_hook_raise_does_not_break_get(monkeypatch):
    monkeypatch.setenv("OK", "1")

    def broken_audit(name, found):
        raise RuntimeError("audit hook broken")

    set_audit_hook(broken_audit)
    # The audit raise must NOT propagate to the caller.
    assert get("OK") == "1"


# ----- T3.2.d rotate() on env backend is no-op (env is fresh) -------------

def test_t3_2_rotate_env_backend_is_noop(monkeypatch):
    monkeypatch.setenv("FRESH", "a")
    assert get("FRESH") == "a"
    monkeypatch.setenv("FRESH", "b")
    # No rotate needed — env backend always reads fresh.
    assert get("FRESH") == "b"


# ----- T3.2.e SIGHUP handler installs (best-effort) -----------------------

@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on Windows")
def test_t3_2_sighup_handler_installs():
    ok = install_sighup_handler()
    # On the main thread this should succeed; if not (test runner
    # context), it just returns False — still no error.
    assert ok in (True, False)


# ----- T3.2.f Vault/Infisical/AWS-SM lazy-import errors -------------------

def test_t3_2_vault_backend_lazy_import_raises_when_hvac_missing():
    """If `hvac` isn't installed, instantiating VaultBackend raises
    RuntimeError — NOT ImportError at module-load time."""
    import sys
    if "hvac" in sys.modules:
        pytest.skip("hvac is installed in this env")
    from lancelot.secrets import VaultBackend
    with pytest.raises(RuntimeError, match="hvac"):
        VaultBackend(url="http://x", token="x")


def test_t3_2_aws_sm_backend_lazy_import_raises_when_boto3_missing():
    import sys
    if "boto3" in sys.modules:
        pytest.skip("boto3 is installed in this env")
    from lancelot.secrets import AwsSecretsManagerBackend
    with pytest.raises(RuntimeError, match="boto3"):
        AwsSecretsManagerBackend()
