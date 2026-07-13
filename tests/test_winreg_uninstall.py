"""The UninstallString written to the Windows registry must actually work.

The 0.2.2 uninstall corrupted real machines twice over: it detected pipx by
searching the *shim* path (which never contains "pipx", so the recommended pipx
install always got the wrong branch, and `pip uninstall` from inside the venv
gutted it while leaving pipx's metadata and the registry entry behind), and
neither branch unregistered, so Settings kept a dead "colabapi" row even when
the package itself was removed.

`_uninstall_command` is a pure function of `exe` and `sys.executable`, so its
contract is testable on every OS:

  * pipx is detected from the interpreter path, never from the shim path;
  * `unregister` runs FIRST (while the exe still exists), then the package
    remover, joined with cmd's conditional `&&` so a failed unregister leaves
    a still-working install rather than a stranded registry entry;
  * paths with spaces stay quoted, under the `cmd /s /c "..."` form whose
    quote handling cmd.exe actually documents.

Run:  python tests/test_winreg_uninstall.py
"""

import sys

from colabapi import winreg_install

failures = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def with_interpreter(fake_executable, exe):
    saved = sys.executable
    sys.executable = fake_executable
    try:
        return winreg_install._uninstall_command(exe)
    finally:
        sys.executable = saved


def test_pipx_detected_from_interpreter_not_shim():
    # The real-world layout: the shim has no "pipx" in it, the interpreter does.
    exe = r"C:\Users\king\.local\bin\colabapi.EXE"
    interp = r"C:\Users\king\pipx\venvs\colabapi\Scripts\python.exe"
    cmd = with_interpreter(interp, exe)
    check("pipx install takes the pipx branch", "pipx uninstall colabapi" in cmd, cmd)
    check("pipx branch never calls pip", "-m pip uninstall" not in cmd, cmd)


def test_shim_path_alone_never_selects_pipx():
    # The inverse trap: a shim path that happens to contain "pipx" must not
    # drag a plain pip install down the pipx branch.
    exe = r"C:\tools\pipx-like\colabapi.exe"
    interp = r"C:\Python313\python.exe"
    cmd = with_interpreter(interp, exe)
    check("plain install takes the pip branch", "-m pip uninstall -y colabapi" in cmd, cmd)
    check("pip branch never calls pipx", "pipx uninstall" not in cmd, cmd)
    check("pip branch uninstalls with the recorded interpreter", interp in cmd, cmd)


def test_unregister_runs_first_in_both_branches():
    exe = r"C:\Users\a user\.local\bin\colabapi.exe"
    for interp, branch in (
        (r"C:\Users\a user\pipx\venvs\colabapi\Scripts\python.exe", "pipx"),
        (r"C:\Program Files\Python313\python.exe", "pip"),
    ):
        cmd = with_interpreter(interp, exe)
        unreg = cmd.find(f'"{exe}" unregister')
        remove = cmd.find("uninstall colabapi" if branch == "pipx" else "-m pip uninstall")
        check(f"{branch}: unregister present and quoted", unreg != -1, cmd)
        check(f"{branch}: unregister runs before package removal",
              unreg != -1 and remove != -1 and unreg < remove, cmd)
        check(f"{branch}: steps chained with cmd's conditional &&", " && " in cmd, cmd)
        check(f"{branch}: uses cmd /s /c with one outer quote pair",
              cmd.startswith('cmd /s /c "') and cmd.endswith('"'), cmd)


if __name__ == "__main__":
    test_pipx_detected_from_interpreter_not_shim()
    test_shim_path_alone_never_selects_pipx()
    test_unregister_runs_first_in_both_branches()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("ALL PASS")
