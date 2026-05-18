import subprocess
import sys


def test_conformance_runner_prints_percent():
    r = subprocess.run(
        [sys.executable, "conformance/report.py"],
        cwd="/Users/seventyleven/Desktop/Lancelot",
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "conformance:" in r.stdout
    pct = float(r.stdout.split("=")[1].strip().rstrip("%"))
    assert 0.0 <= pct <= 100.0
