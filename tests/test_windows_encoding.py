"""The CLI must survive a legacy-code-page stdout without a single traceback.

On Windows, output that is redirected, piped, or captured (a log file, an IDE,
CI, the Scheduled Task the service installs) is encoded with the legacy ANSI
code page -- cp1252 -- which cannot represent the glyphs colabapi prints
(arrows, dots, prompt chevrons). Before 0.2.1 that was a UnicodeEncodeError
crash in six modules; main() now reconfigures the streams to UTF-8 first.

This test recreates the hostile environment on every OS: run the CLI with
output CAPTURED (that is what forces the non-console encoding path) and
PYTHONIOENCODING=cp1252 (so even a POSIX runner encodes like a legacy Windows
pipe). `doctor` is the command under test because it is non-interactive, runs
on every platform, and prints through the same Rich console as everything else.

Run:  python tests/test_windows_encoding.py
"""

import os
import subprocess
import sys

failures = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "colabapi", *args],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
    )


def test_doctor_under_cp1252():
    r = run_cli("doctor")
    check("doctor exits 0 with captured cp1252 output", r.returncode == 0,
          (r.stderr or r.stdout).strip()[-300:])
    check("doctor prints no traceback", "Traceback" not in r.stderr, r.stderr[-300:])


def test_version_under_cp1252():
    r = run_cli("--version")
    check("--version exits 0 with captured cp1252 output", r.returncode == 0,
          (r.stderr or r.stdout).strip()[-300:])


def test_help_under_cp1252():
    r = run_cli("--help")
    check("--help exits 0 with captured cp1252 output", r.returncode == 0,
          (r.stderr or r.stdout).strip()[-300:])


if __name__ == "__main__":
    test_doctor_under_cp1252()
    test_version_under_cp1252()
    test_help_under_cp1252()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("ALL PASS")
