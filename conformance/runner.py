"""Run the vendored corpus through the swap; return (passed, total).
M1-P7 swaps the seed for the pinned 581-file FastAPI tree + manifests."""
import contextlib
import io
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run() -> tuple[int, int]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import conformance.conftest  # noqa: F401  (applies the swap)
    import pytest

    vendor = ROOT / "conformance" / "vendor"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        pytest.main(["-q", "-p", "no:cacheprovider", str(vendor)])
    out = buf.getvalue()
    passed = sum(int(n) for n in re.findall(r"(\d+) passed", out))
    failed = sum(int(n) for n in re.findall(r"(\d+) failed", out))
    errors = sum(int(n) for n in re.findall(r"(\d+) error", out))
    total = passed + failed + errors
    return passed, max(total, 1)
