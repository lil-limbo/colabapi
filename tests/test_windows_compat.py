"""Verify that Google's Colab CLI can be imported and driven on Windows.

Google's CLI is Linux/macOS only because `colab_cli/console.py` imports the
POSIX-only `termios` and `tty` at module scope, and `commands/execution.py`
imports *that* at module scope -- so on Windows `import colab_cli.cli` raises
ImportError before any command runs. `colabapi._winshim` supplies those two
modules via the Win32 console API.

On Windows this exercises the real shim. On Linux/macOS the real modules exist,
so we simulate their absence and check that a module of the shim's shape is
enough -- which keeps the contract honest even when CI has no Windows runner.

Run:  python tests/test_windows_compat.py
"""

import subprocess
import sys
import types

BLOCKED = {"termios", "tty"}
IS_WINDOWS = sys.platform == "win32"

failures = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def drop_colab_cli():
    for mod in [m for m in sys.modules if m.split(".")[0] == "colab_cli"]:
        del sys.modules[mod]


class Blocker:
    """Make termios/tty unimportable, simulating Windows on a POSIX box.

    Implements find_spec, not find_module: Python 3.12 dropped the legacy finder
    protocol, so a find_module-based blocker is silently ignored and blocks
    nothing (which would make this test pass for the wrong reason).
    """

    def find_spec(self, name, path=None, target=None):
        if name in BLOCKED:
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return None


# --------------------------------------------------------------------------
# 1. The premise: without termios/tty, Google's CLI cannot be imported.
# --------------------------------------------------------------------------
if IS_WINDOWS:
    drop_colab_cli()
    for m in BLOCKED:
        sys.modules.pop(m, None)
    try:
        import colab_cli.cli  # noqa: F401
        check("Google's CLI fails without the shim", False,
              "it imported unaided -- Google may have fixed Windows support")
    except ImportError as exc:
        check("Google's CLI fails without the shim", True, str(exc))
else:
    drop_colab_cli()
    for m in BLOCKED:
        sys.modules.pop(m, None)
    sys.meta_path.insert(0, Blocker())
    try:
        import colab_cli.cli  # noqa: F401
        check("Google's CLI fails without termios", False, "imported anyway")
    except ImportError as exc:
        check("Google's CLI fails without termios", True, str(exc))

# --------------------------------------------------------------------------
# 2. With the shim in place, it imports.
# --------------------------------------------------------------------------
drop_colab_cli()

if IS_WINDOWS:
    from colabapi import _winshim

    installed = _winshim.install()
    check("_winshim.install() registers the shim", installed)
else:
    # POSIX: stand in modules with exactly the shim's public surface.
    termios = types.ModuleType("termios")
    termios.tcgetattr = lambda fd: 0
    termios.tcsetattr = lambda fd, when, attrs: None
    termios.error = type("error", (Exception,), {})
    termios.TCSANOW, termios.TCSADRAIN, termios.TCSAFLUSH = 0, 1, 2
    tty = types.ModuleType("tty")
    tty.setraw = lambda fd, when=0: None
    tty.setcbreak = lambda fd, when=0: None
    sys.modules["termios"], sys.modules["tty"] = termios, tty

try:
    import colab_cli.cli  # noqa: F401
    check("colab_cli.cli imports with the shim", True)
except ImportError as exc:
    check("colab_cli.cli imports with the shim", False, str(exc))

# --------------------------------------------------------------------------
# 3. Every termios/tty symbol console.py touches must exist, or it will import
#    fine and then explode the moment a user opens a shell.
# --------------------------------------------------------------------------
try:
    import colab_cli.console as cons

    missing = [
        f"termios.{a}" for a in ("tcgetattr", "tcsetattr", "TCSANOW")
        if not hasattr(cons.termios, a)
    ] + [f"tty.{a}" for a in ("setraw",) if not hasattr(cons.tty, a)]
    check("console.py's full termios/tty surface is covered", not missing,
          f"missing: {missing}" if missing else "")
except Exception as exc:  # noqa: BLE001
    check("console.py's full termios/tty surface is covered", False, str(exc))

# --------------------------------------------------------------------------
# 4. End to end: the shim entry point actually runs Google's CLI.
# --------------------------------------------------------------------------
sys.meta_path = [m for m in sys.meta_path if not isinstance(m, Blocker)]
proc = subprocess.run(
    [sys.executable, "-m", "colabapi._colab_shim", "--help"],
    capture_output=True, text=True, timeout=120,
)
out = (proc.stdout or "") + (proc.stderr or "")
check("`python -m colabapi._colab_shim --help` runs Google's CLI",
      proc.returncode == 0 and "Usage" in out,
      f"rc={proc.returncode}")

# --------------------------------------------------------------------------
# 5. Windows only: the console-mode plumbing must not throw. (Under CI stdio is
#    redirected, so these return False rather than flipping real console bits --
#    the point is that they degrade quietly instead of raising.)
# --------------------------------------------------------------------------
if IS_WINDOWS:
    from colabapi import _winshim

    try:
        _winshim.enable_vt_mode()
        import termios as t
        import tty as y

        y.setraw(0)
        y.setcbreak(0)
        try:
            saved = t.tcgetattr(0)
            t.tcsetattr(0, t.TCSANOW, saved)
        except t.error:
            pass  # stdin redirected under CI: expected, and handled
        check("Win32 console plumbing runs without raising", True)
    except Exception as exc:  # noqa: BLE001
        check("Win32 console plumbing runs without raising", False, repr(exc))

    import signal

    check("SIGWINCH exists on Windows after the shim", hasattr(signal, "SIGWINCH"))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL PASS")
