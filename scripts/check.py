"""Run everything CI runs, in the same order: lint, test formatting, tests
with coverage. The one command to run before opening a pull request.

Usage: python scripts/check.py [extra pytest arguments]

Stops at the first step that fails and exits with its status. Anything
after the script name is passed on to pytest (e.g. `-x`, `-k something`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def steps(pytest_arguments: list[str]) -> list[tuple[str, list[str]]]:
    python = [sys.executable, "-m"]
    return [
        ("lint", [*python, "ruff", "check", "."]),
        ("test formatting", [*python, "ruff", "format", "--check", "tests"]),
        ("tests", [*python, "pytest", "--cov", "--cov-report=term-missing:skip-covered", *pytest_arguments]),
    ]


def main(arguments: list[str]) -> int:
    for name, command in steps(arguments):
        print(f"\n== {name}: {' '.join(command[2:])}", flush=True)
        status = subprocess.run(command, cwd=ROOT).returncode
        if status != 0:
            print(f"\n{name} failed (exit status {status})")
            return status
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
