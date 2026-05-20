"""CI ratchet — gates on **absolute passing test count**, NOT on pct.

Why passes-floor, not pct-floor (decision M1-EXIT-X5):
The pct metric drops when collection-unblocks (e.g. UploadFile) cause the
denominator to grow faster than the numerator — even when real progress
is made. That is a denominator artifact, not a regression. The honest
no-bullshit gate is "the count of REAL FastAPI tests passing only goes
up." Denominator-agnostic. Monotone always. Cannot be gamed by adding
exclusions (those would reduce passes_total but ALSO reduce passes, so
both move together).

`parity_floor.txt` format:
    passes=574                   # the floor: passes >= this number
(legacy integer-only floors are interpreted as pct for backward compat.)

Anti-cheat: still runs `classify.py --check` so a behavioral test cannot
be parked in `exclude.toml` to inflate the count."""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from conformance.runner import run

ROOT = pathlib.Path(__file__).resolve().parent


def _read_floor():
    raw = (ROOT / "parity_floor.txt").read_text().strip()
    if raw.startswith("passes="):
        return ("passes", int(raw.split("=", 1)[1]))
    # Legacy integer = pct floor.
    return ("pct", float(raw))


rc = subprocess.run(
    [sys.executable, "conformance/classify.py", "--check"],
    cwd=str(ROOT.parent)).returncode
if rc != 0:
    print("RATCHET FAIL: anti-cheat (exclude.toml drifted from classifier)")
    sys.exit(1)

passed, total = run()
pct = 100.0 * passed / total
kind, floor_value = _read_floor()
print(f"conformance Class-A: {passed}/{total} = {pct:.1f}%  "
      f"(floor {kind}={floor_value})")
if kind == "passes":
    if passed < floor_value:
        print(f"RATCHET FAIL: {passed} passes < floor {floor_value} "
              "(real regression — fewer FastAPI tests pass)")
        sys.exit(1)
else:  # legacy pct-floor
    if pct + 1e-9 < floor_value:
        print("RATCHET FAIL: parity regressed below the measured floor")
        sys.exit(1)
sys.exit(0)
