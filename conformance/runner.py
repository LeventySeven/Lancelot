"""Run the vendored corpus through the swap; return (passed, total).

A6: runs pytest in a SUBPROCESS (not in-process) so an in-process caller
(e.g. tests/test_m1_p1_acceptance.py) never pollutes the parent pytest
process's sys.modules['fastapi'] with the swap fake. The swap auto-applies
because pytest loads `conformance/conftest.py` (the parent conftest of
`conformance/vendor`). M1-P7 swaps the seed for the pinned 581-file FastAPI
tree + reference/candidate differential harness."""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run() -> tuple[int, int]:
    vendor = ROOT / "conformance" / "vendor"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(vendor)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    out = proc.stdout + proc.stderr
    passed = sum(int(n) for n in re.findall(r"(\d+) passed", out))
    failed = sum(int(n) for n in re.findall(r"(\d+) failed", out))
    errors = sum(int(n) for n in re.findall(r"(\d+) error", out))
    total = passed + failed + errors
    return passed, max(total, 1)
