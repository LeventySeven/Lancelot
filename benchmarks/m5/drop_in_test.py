"""M5 T2.b — drop-in compatibility test harness for real FastAPI apps.

Given the path to a FastAPI app (a directory containing the app's source +
pyproject.toml or requirements.txt + a test suite), this harness:

  1. Snapshots the original source.
  2. Applies the documented import-swap (`from fastapi import X` →
     `from lancelot import X` for the surface covered in
     docs/migration/from-fastapi.md).
  3. Runs `pytest` (or a user-specified test command) in the swapped tree.
  4. Captures pass/fail + per-test failure root cause.
  5. Restores the original source.
  6. Emits a JSON summary report.

Usage:
    python drop_in_test.py /path/to/some-fastapi-app \\
        [--test-cmd "pytest -x -q"] \\
        [--report /tmp/dropin-app1.json]

The report's `outcome` field is:
  passed_unmodified  — zero patches needed; tests green
  passed_with_patches — tests green after diff applied (diff captured)
  failed_no_diff      — tests fail; not a Lancelot bug we can patch around
  framework_error     — Lancelot itself raises on import/setup (a real bug)

Designed to be run against the K=3 suggested OSS apps from M5-PLAN T2.b.1:
  - tiangolo's full-stack-fastapi-template
  - a streaming-agent app (LangGraph examples or similar)
  - a LiteLLM-shape LLM proxy

But the harness is general; point it at any FastAPI repo with a test suite.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


# The lines we swap. Conservative — only the import surface we explicitly
# advertise in docs/migration/from-fastapi.md. Anything else is left alone
# so the test surface accurately tells us what's missing.
IMPORT_SWAPS: list[tuple[re.Pattern, str]] = [
    # `from fastapi import X[, Y, …]` → `from lancelot import X[, Y, …]`
    (re.compile(r"^from\s+fastapi\s+import\s+", re.MULTILINE), "from lancelot import "),
    # `from fastapi.responses import X` → `from lancelot.responses import X`
    (re.compile(r"^from\s+fastapi\.responses\s+import\s+", re.MULTILINE),
     "from lancelot.responses import "),
    # `from fastapi.requests import Request` → `from lancelot.requests import Request`
    (re.compile(r"^from\s+fastapi\.requests\s+import\s+", re.MULTILINE),
     "from lancelot.requests import "),
    # `from fastapi.routing import APIRouter` etc.
    (re.compile(r"^from\s+fastapi\.routing\s+import\s+", re.MULTILINE),
     "from lancelot.routing import "),
    # `import fastapi` → `import lancelot as fastapi` (last resort; preserves
    # bare attribute access)
    (re.compile(r"^import\s+fastapi\s*$", re.MULTILINE), "import lancelot as fastapi"),
]

# Filenames that should NOT be edited (build outputs, vendored libs, etc.).
SKIP_FILE_PATTERNS = [
    re.compile(r"\.git/"),
    re.compile(r"__pycache__/"),
    re.compile(r"\.venv/"),
    re.compile(r"site-packages/"),
    re.compile(r"node_modules/"),
    re.compile(r"\.pytest_cache/"),
]


@dataclass
class FileSwap:
    path: str
    n_swaps: int
    original_sha256: str
    swapped_sha256: str


@dataclass
class Report:
    app_path: str
    test_cmd: str
    outcome: str = ""           # see module docstring for set of values
    n_files_changed: int = 0
    n_swaps_applied: int = 0
    swaps: list[FileSwap] = field(default_factory=list)
    test_returncode: int = -1
    test_stdout_tail: str = ""
    test_stderr_tail: str = ""
    duration_s: float = 0.0
    framework_error: Optional[str] = None


def sha256(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()


def should_skip(path: Path) -> bool:
    s = str(path)
    return any(p.search(s) for p in SKIP_FILE_PATTERNS) or not path.suffix == ".py"


def apply_swaps(work_dir: Path) -> tuple[int, int, list[FileSwap]]:
    """Walk work_dir, apply IMPORT_SWAPS to every .py file. Returns
    (n_files_changed, n_swaps_total, swap_records)."""
    swap_records: list[FileSwap] = []
    n_files = 0
    n_swaps_total = 0

    for path in work_dir.rglob("*.py"):
        if should_skip(path):
            continue
        try:
            content = path.read_bytes()
        except Exception:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue

        new_text = text
        swaps_in_file = 0
        for pat, repl in IMPORT_SWAPS:
            new_text, n = pat.subn(repl, new_text)
            swaps_in_file += n
        if swaps_in_file == 0:
            continue

        new_bytes = new_text.encode("utf-8")
        path.write_bytes(new_bytes)
        swap_records.append(FileSwap(
            path=str(path.relative_to(work_dir)),
            n_swaps=swaps_in_file,
            original_sha256=sha256(content),
            swapped_sha256=sha256(new_bytes),
        ))
        n_files += 1
        n_swaps_total += swaps_in_file

    return n_files, n_swaps_total, swap_records


def run_test_command(work_dir: Path, test_cmd: str, env_extra: dict) -> tuple[int, str, str]:
    env = dict(os.environ)
    env.update(env_extra)
    # Run with a 10-minute deadline — tests should not take longer.
    try:
        proc = subprocess.run(
            test_cmd, shell=True, cwd=str(work_dir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600,
        )
    except subprocess.TimeoutExpired as e:
        return (124, (e.stdout or b"").decode(errors="replace"),
                (e.stderr or b"").decode(errors="replace") + "\n[TIMEOUT after 600s]")
    return (proc.returncode,
            proc.stdout.decode(errors="replace"),
            proc.stderr.decode(errors="replace"))


def tail(s: str, n_lines: int = 80) -> str:
    lines = s.splitlines()
    return "\n".join(lines[-n_lines:])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("app_path", help="path to a FastAPI app directory")
    ap.add_argument("--test-cmd", default="pytest -x -q",
                    help="how to run tests (default: pytest -x -q)")
    ap.add_argument("--report", default=None, help="write JSON report here")
    ap.add_argument("--keep-workdir", action="store_true",
                    help="don't delete the temp work dir at end (for debugging)")
    args = ap.parse_args()

    src = Path(args.app_path).resolve()
    if not src.is_dir():
        print(f"ERROR: not a directory: {src}", file=sys.stderr)
        return 2

    report = Report(app_path=str(src), test_cmd=args.test_cmd)
    t0 = time.monotonic()

    # 1. Copy app to temp work dir (don't mutate user's source).
    work = Path(tempfile.mkdtemp(prefix="lancelot-dropin-"))
    try:
        # Skip large irrelevant dirs in the copy.
        def _ignore(_d, names):
            return [n for n in names
                    if n in {".git", "__pycache__", ".venv", "node_modules",
                             ".pytest_cache", "dist", "build"}]
        shutil.copytree(src, work / "app", ignore=_ignore)
        work_app = work / "app"
        print(f"copied app: {src} → {work_app}")

        # 2. Apply swaps.
        n_files, n_swaps, swaps = apply_swaps(work_app)
        report.n_files_changed = n_files
        report.n_swaps_applied = n_swaps
        report.swaps = swaps
        print(f"applied {n_swaps} swaps across {n_files} files")

        # 3. Run tests.
        print(f"running: {args.test_cmd}  (cwd={work_app})")
        rc, out, err = run_test_command(work_app, args.test_cmd, env_extra={})
        report.test_returncode = rc
        report.test_stdout_tail = tail(out)
        report.test_stderr_tail = tail(err)

        # 4. Classify outcome.
        if rc == 0 and n_swaps == 0:
            report.outcome = "passed_unmodified"
        elif rc == 0:
            report.outcome = "passed_with_patches"
        else:
            # Distinguish framework-level errors from test failures.
            framework_signals = (
                "ImportError", "AttributeError", "ModuleNotFoundError",
                "TypeError", "lancelot.exceptions.FastAPIError",
            )
            if any(sig in err for sig in framework_signals):
                report.outcome = "framework_error"
                report.framework_error = next(
                    (line for line in err.splitlines() if any(s in line for s in framework_signals)),
                    None
                )
            else:
                report.outcome = "failed_no_diff"

        report.duration_s = time.monotonic() - t0

        # 5. Emit report.
        print()
        print("=" * 60)
        print(f"app: {src}")
        print(f"outcome: {report.outcome}")
        print(f"swaps:   {n_swaps} swaps across {n_files} files")
        print(f"tests:   exit={rc}  ({report.duration_s:.1f}s)")
        if report.framework_error:
            print(f"framework_error: {report.framework_error}")
        print()
        print("--- test stdout tail ---")
        print(report.test_stdout_tail or "(empty)")
        print()
        if rc != 0:
            print("--- test stderr tail ---")
            print(report.test_stderr_tail or "(empty)")

        if args.report:
            with open(args.report, "w") as f:
                json.dump(asdict(report), f, indent=2)
            print(f"\nreport written: {args.report}")

        # Exit code: 0 if passed (unmodified or with patches), 1 if failed.
        return 0 if report.outcome.startswith("passed") else 1

    finally:
        if not args.keep_workdir:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"work dir kept: {work}")


if __name__ == "__main__":
    sys.exit(main())
