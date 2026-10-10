"""Hardware-free verification: syntax checks, unit tests, optional frontend tests.

Runs without a CD drive, a music library, Bluetooth hardware or a live MPD.
Requires Python 3.10+; the frontend suite additionally requires Node.js 18+.
"""
import argparse
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--python-only", action="store_true",
                    help="Omit the frontend suite when Node.js is unavailable")
args = parser.parse_args()

for source in (sorted(SRC.rglob("*.py")) + sorted((ROOT / "tests").glob("*.py"))
               + sorted((ROOT / "scripts").glob("*.py"))):
    ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

environment = dict(os.environ)
environment["PYTHONPATH"] = os.pathsep.join(
    [str(SRC), str(ROOT), environment.get("PYTHONPATH", "")]).rstrip(os.pathsep)

subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
               cwd=ROOT, env=environment, check=True)

if not args.python_only:
    node = shutil.which("node")
    if node is None:
        raise SystemExit(
            "Node.js 18+ is required for the frontend suite; "
            "pass --python-only to skip it explicitly.")
    subprocess.run([node, "--test", "tests/frontend/ui.test.cjs"], cwd=ROOT, check=True)

print("Verification passed" + (" (Python only)" if args.python_only else " (Python + frontend)"))
