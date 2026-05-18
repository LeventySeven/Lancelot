import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from conformance.runner import run

passed, total = run()
pct = 100.0 * passed / total
print(f"conformance: {passed}/{total} = {pct:.1f}%")
sys.exit(0)
