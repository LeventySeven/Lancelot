"""CI ratchet: parity may only rise. `parity_floor.txt` is the TRUE
measured Class-A % at the m1-p7-green baseline (an HONEST floor, not a
target); the gate fails if pct drops below it. Anti-cheat: also runs
`classify.py --check` so a behavioral test can never be parked in
`exclude.toml` to inflate the ratio (harness-design §5)."""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from conformance.runner import run

ROOT = pathlib.Path(__file__).resolve().parent
floor = float((ROOT / "parity_floor.txt").read_text().strip())

rc = subprocess.run(
    [sys.executable, "conformance/classify.py", "--check"],
    cwd=str(ROOT.parent)).returncode
if rc != 0:
    print("RATCHET FAIL: anti-cheat (exclude.toml drifted from classifier)")
    sys.exit(1)

passed, total = run()
pct = 100.0 * passed / total
print(f"conformance Class-A: {passed}/{total} = {pct:.1f}%  "
      f"(floor {floor:.1f}%)")
if pct + 1e-9 < floor:
    print("RATCHET FAIL: parity regressed below the measured floor")
    sys.exit(1)
sys.exit(0)
