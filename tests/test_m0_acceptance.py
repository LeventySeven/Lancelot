"""M0 DoD #1: `import lancelot` works under py3.13 AND py3.14 (free-threaded
`*t` is CI-only — skipped locally if absent).

The plan's literal version ran `python3.14 -c import lancelot` against the
*system* interpreter (package not installed) — a guaranteed false failure.
A venv+pip approach is also fragile (the local python3.14's `ensurepip` is
broken). The robust, dependency-free proof: a wheel is a zip — extract the
built abi3 wheel and `import lancelot` via PYTHONPATH under each interpreter.
`import lancelot; _ping()` needs no third-party deps (pydantic-core is imported
lazily only at serialize time), so this directly tests "does the abi3 extension
load under interpreter X" with zero pip/venv machinery.
"""

import glob
import os
import subprocess
import shutil
import tempfile
import zipfile

import pytest


def test_native_module_loads():
    import lancelot

    assert lancelot._ping() == "lancelot-m0"


def _newest_wheel():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    wheels = glob.glob(os.path.join(here, "dist", "*.whl"))
    return max(wheels, key=os.path.getmtime) if wheels else None


def _assert_imports_under(exe):
    if not shutil.which(exe):
        pytest.skip(f"{exe} not present")
    wheel = _newest_wheel()
    if not wheel:
        pytest.skip("no built wheel in dist/ — run `make m0` for the full gate")
    with tempfile.TemporaryDirectory() as d:
        with zipfile.ZipFile(wheel) as z:
            z.extractall(d)
        env = {**os.environ, "PYTHONPATH": d}
        out = subprocess.run(
            [exe, "-c", "import lancelot; print(lancelot._ping())"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        assert out.stdout.strip() == "lancelot-m0"


@pytest.mark.parametrize("exe", ["python3.13", "python3.14"])
def test_abi3_wheel_imports_under_interpreter(exe):
    _assert_imports_under(exe)


@pytest.mark.parametrize("exe", ["python3.13t", "python3.14t"])
def test_free_threaded_import_ci_only(exe):
    # Free-threaded import is enforced in CI; skipped locally when absent.
    _assert_imports_under(exe)
