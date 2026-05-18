"""CI ratchet: parity may only rise. floor in parity_floor.txt (seed 0).
M1-P7 wires this into `make m1` with the real >=90 gate."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from conformance.runner import run

ROOT = pathlib.Path(__file__).resolve().parent
floor_file = ROOT / "parity_floor.txt"
floor = float(floor_file.read_text().strip()) if floor_file.exists() else 0.0

passed, total = run()
pct = 100.0 * passed / total
print(f"conformance: {passed}/{total} = {pct:.1f}%  (floor {floor:.1f}%)")
if pct + 1e-9 < floor:
    print("RATCHET FAIL: parity regressed below floor")
    sys.exit(1)
sys.exit(0)
