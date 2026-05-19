"""Run the pinned real FastAPI corpus through the swap; return
(passed, total) over **Class A only** (the honest gate).

A6: pytest runs in a SUBPROCESS so the swap fake `fastapi` never leaks
into a parent pytest. `conformance/conftest.py` (the swap) auto-applies
because it is the rootdir conftest (cwd=conformance). `vendor/fastapi`
is on PYTHONPATH so `from .main import app` (the `tests` package) and
`from docs_src... import app` resolve against the pinned tree, byte-
unmodified. classify.py decides Class A/A2/B/EXCLUDE deterministically;
no vendored test is ever edited and no file is hand-picked to move the
ratio (harness-design §4 — the cardinal rule)."""
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
VENDOR_FA = ROOT / "vendor" / "fastapi"


def _class_a_files():
    sys.path.insert(0, str(ROOT.parent))
    from conformance.classify import classify_all

    return [str(VENDOR_FA / rel)
            for rel, (tag, _r) in sorted(classify_all().items())
            if tag == "A"]


def run() -> tuple[int, int]:
    files = _class_a_files()
    if not files:
        return 0, 1
    env = {**os.environ, "PYTHONPATH": str(VENDOR_FA)}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--no-header", "--import-mode=importlib",
         "--continue-on-collection-errors", *files],
        cwd=str(ROOT), capture_output=True, text=True, env=env,
    )
    out = proc.stdout + proc.stderr
    passed = sum(int(n) for n in re.findall(r"(\d+) passed", out))
    failed = sum(int(n) for n in re.findall(r"(\d+) failed", out))
    errors = sum(int(n) for n in re.findall(r"(\d+) error", out))
    return passed, max(passed + failed + errors, 1)


if __name__ == "__main__":
    p, t = run()
    print(f"Class-A: {p}/{t} = {100.0 * p / t:.1f}%")
