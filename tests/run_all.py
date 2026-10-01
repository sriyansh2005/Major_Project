"""Run every test suite:  python tests/run_all.py   (from the project root)"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
failed = []
for t in sorted(HERE.glob("test_*.py")):
    r = subprocess.run([sys.executable, str(t)], capture_output=True, text=True, cwd=HERE.parent)
    passes = r.stdout.count("  PASS")
    status = "ok" if r.returncode == 0 else "FAILED"
    print(f"{t.name:22s} {status:7s} {passes} checks")
    if r.returncode:
        failed.append(t.name)
        print(r.stdout[-2000:], r.stderr[-2000:])
print("\nALL SUITES PASSED" if not failed else f"\nFAILED: {failed}")
sys.exit(1 if failed else 0)
