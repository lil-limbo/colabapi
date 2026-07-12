"""Make Google's official Colab CLI importable and usable on Windows.

Google states the Colab CLI supports Linux and macOS only. The reason turns out
to be shallow rather than architectural: `colab_cli/console.py` does

    import termios
    import tty

at module scope, and `colab_cli/commands/execution.py` imports that module at
*its* module scope, which `colab_cli/cli.py` in turn imports. Both modules are
POSIX-only, so on Windows `import colab_cli` raises ImportError before any
command can run -- not just the terminal, but `new`, `stop`, `exec`, everything.

The transport underneath is a plain WebSocket (`wss://.../colab/tty`), which is
perfectly cross-platform. termios/tty are used *only* to put the local terminal
into raw mode and to catch window resizes. Windows can do both, just through a
different API: GetConsoleMode/SetConsoleMode.

So this module synthesises the two missing stdlib modules, backed by the Win32
console API via ctypes, and registers them in `sys.modules` *before* colab_cli is
imported. Google's code then imports and calls them unmodified. We patch nothing
inside colab_cli itself, which means their updates keep working.

`install()` is a no-op on POSIX, so callers can invoke it unconditionally.
"""

from __future__ import annotations

import os
import struct
import sys
import threading
import types

IS_WINDOWS = os.name == "nt"

# -- Win32 console constants ------------------------------------------------
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11

ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

ENABLE_PROCESSED_OUTPUT = 0x0001
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

# termios.tcsetattr's `when` argument. The values are arbitrary here (nothing on
# Windows consumes them); they exist so Google's `termios.TCSANOW` resolves.
TCSANOW = 0
TCSADRAIN = 1
TCSAFLUSH = 2

# Signal number we invent for SIGWINCH. Windows has no such signal, and the real
# signal module has no attribute for it, so console.py's
# `signal.signal(signal.SIGWINCH, ...)` would raise AttributeError. We expose a
# value that no real Windows signal uses and intercept it in our signal wrapper.
FAKE_SIGWINCH = 28  # SIGWINCH's number on Linux, for what little it's worth.

_installed = False
_resize_watcher: "_ResizeWatcher | None" = None

_k32 = None


def _kernel32():
    """kernel32 with argtypes/restype declared, loaded once.

    Without declarations ctypes assumes every return value is a 32-bit c_int.
    GetStdHandle returns a HANDLE, which is pointer-sized on 64-bit Windows:
    letting it be truncated to c_int and then re-widened through the default
    int conversion happens to work for console handles (they are 32-bit
    significant and sign-extended) but is an ABI accident, not a contract.
    Declaring HANDLE/DWORD once here makes the calls correct by construction.
    """
    global _k32
    if _k32 is not None:
        return _k32
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetStdHandle.argtypes = [wintypes.DWORD]
    k32.GetStdHandle.restype = wintypes.HANDLE
    k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetConsoleMode.restype = wintypes.BOOL
    k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.SetConsoleMode.restype = wintypes.BOOL
    _k32 = k32
    return k32


# GetStdHandle's INVALID_HANDLE_VALUE, as the unsigned pointer-sized value
# ctypes returns once HANDLE is declared (c_void_p -> Python int or None).
_INVALID_HANDLE_VALUE = 2 ** (8 * struct.calcsize("P")) - 1


def _get_console_mode(handle_id: int):
    """Return the current console mode, or None if this isn't a real console.

    Returns None when stdio is redirected to a pipe or file, which is normal
    (e.g. `colabapi exec ... | findstr foo`) and must not be treated as an error.
    """
    import ctypes
    from ctypes import wintypes

    k32 = _kernel32()
    handle = k32.GetStdHandle(wintypes.DWORD(handle_id & 0xFFFFFFFF))
    # With restype=HANDLE (c_void_p), NULL comes back as None and
    # INVALID_HANDLE_VALUE as the all-ones pointer value.
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        return None
    mode = wintypes.DWORD()
    if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
        return None  # not a console (redirected)
    return handle, mode.value


def _set_console_mode(handle, mode: int) -> bool:
    return bool(_kernel32().SetConsoleMode(handle, mode))


def enable_vt_mode() -> bool:
    """Turn on ANSI escape-sequence rendering for stdout.

    The Colab backend streams raw ANSI (colours, cursor moves) straight from the
    remote shell. Windows Terminal and PowerShell 7 render that by default;
    legacy conhost (older cmd.exe) shows the escape codes as literal garbage
    unless VIRTUAL_TERMINAL_PROCESSING is switched on. Returns True if ANSI will
    render.
    """
    if not IS_WINDOWS:
        return True
    got = _get_console_mode(STD_OUTPUT_HANDLE)
    if not got:
        return False
    handle, mode = got
    return _set_console_mode(
        handle, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_PROCESSED_OUTPUT
    )


# -- raw mode ----------------------------------------------------------------
def _tcgetattr(fd):  # noqa: ARG001 - fd is ignored; Windows uses the std handle
    """Snapshot the console mode so it can be restored later.

    Google's code treats the return value as opaque and only ever hands it back
    to tcsetattr, so returning the raw mode integer (rather than the 7-element
    list POSIX termios returns) is safe and keeps this simple.
    """
    got = _get_console_mode(STD_INPUT_HANDLE)
    if not got:
        raise _TermiosError("stdin is not a console")
    return got[1]


def _tcsetattr(fd, when, attributes):  # noqa: ARG001
    got = _get_console_mode(STD_INPUT_HANDLE)
    if not got:
        return
    handle, _ = got
    _set_console_mode(handle, int(attributes))


def _setraw(fd, when=TCSANOW):  # noqa: ARG001
    """Windows equivalent of tty.setraw().

    Raw means: no line buffering (deliver each keypress immediately), no local
    echo (the remote shell echoes for us), and no Ctrl+C interception -- Ctrl+C
    must travel to the *remote* process as byte 0x03, exactly as POSIX raw mode
    does, instead of raising KeyboardInterrupt locally.

    VIRTUAL_TERMINAL_INPUT makes the console encode arrow keys, function keys
    and friends as the ANSI sequences the remote shell already understands, so
    we do not have to translate Windows key events by hand.
    """
    got = _get_console_mode(STD_INPUT_HANDLE)
    if not got:
        return
    handle, mode = got
    mode &= ~(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT)
    mode |= ENABLE_VIRTUAL_TERMINAL_INPUT
    _set_console_mode(handle, mode)
    enable_vt_mode()  # ANSI coming back from the runtime must render, too.


def _setcbreak(fd, when=TCSANOW):  # noqa: ARG001
    """cbreak: unbuffered and unechoed, but Ctrl+C still interrupts locally."""
    got = _get_console_mode(STD_INPUT_HANDLE)
    if not got:
        return
    handle, mode = got
    mode &= ~(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT)
    mode |= ENABLE_PROCESSED_INPUT | ENABLE_VIRTUAL_TERMINAL_INPUT
    _set_console_mode(handle, mode)


class _TermiosError(Exception):
    """Stand-in for termios.error."""


# -- SIGWINCH emulation ------------------------------------------------------
class _ResizeWatcher(threading.Thread):
    """Poll the console size and invoke the handler when it changes.

    Windows delivers window-resize as a console *input event*, not a signal, and
    consuming those events would fight with the keystroke reader. Polling the
    reported size once a second is cheap, has no such conflict, and is plenty
    responsive for the one thing this drives: telling the remote pty its new
    dimensions so redraws are not mangled.
    """

    def __init__(self):
        super().__init__(name="colabapi-winresize", daemon=True)
        self._handler = None
        self._stop = threading.Event()
        self._last = self._size()

    @staticmethod
    def _size():
        try:
            s = os.get_terminal_size()
            return (s.columns, s.lines)
        except OSError:
            return None

    def set_handler(self, handler) -> None:
        self._handler = handler

    def run(self) -> None:
        while not self._stop.wait(1.0):
            handler = self._handler
            if handler is None:
                continue
            size = self._size()
            if size and size != self._last:
                self._last = size
                try:
                    handler(FAKE_SIGWINCH, None)
                except Exception:  # noqa: BLE001 - a resize must never kill the session
                    pass

    def stop(self) -> None:
        self._stop.set()


def _patch_signal() -> None:
    """Teach the stdlib `signal` module about SIGWINCH on Windows.

    console.py calls `signal.signal(signal.SIGWINCH, handle_sigwinch)` and, in
    its finally block, resets it to SIG_DFL. Neither the attribute nor the
    signal exists here, so we add the attribute and wrap `signal.signal` to
    route that one number to the resize-polling thread, delegating every other
    signal to the real implementation untouched.
    """
    global _resize_watcher
    import signal as _signal

    if getattr(_signal, "_colabapi_patched", False):
        return

    _signal.SIGWINCH = FAKE_SIGWINCH  # type: ignore[attr-defined]
    _real_signal = _signal.signal
    _resize_watcher = _ResizeWatcher()
    _resize_watcher.start()

    def signal_shim(signum, handler):
        if signum == FAKE_SIGWINCH:
            watcher = _resize_watcher
            if watcher is not None:
                # SIG_DFL/SIG_IGN mean "stop telling me about resizes".
                watcher.set_handler(
                    None if handler in (_signal.SIG_DFL, _signal.SIG_IGN) else handler
                )
            return _signal.SIG_DFL
        return _real_signal(signum, handler)

    _signal.signal = signal_shim  # type: ignore[assignment]
    _signal._colabapi_patched = True  # type: ignore[attr-defined]


# -- public entry point ------------------------------------------------------
def install() -> bool:
    """Register the fake termios/tty modules. Call before importing colab_cli.

    No-op on POSIX (where the real modules exist) and idempotent, so it is safe
    to call from several entry points. Returns True if a shim was installed.
    """
    global _installed
    if not IS_WINDOWS or _installed:
        return False

    termios = types.ModuleType("termios")
    termios.tcgetattr = _tcgetattr
    termios.tcsetattr = _tcsetattr
    termios.error = _TermiosError
    termios.TCSANOW = TCSANOW
    termios.TCSADRAIN = TCSADRAIN
    termios.TCSAFLUSH = TCSAFLUSH
    termios.__doc__ = "colabapi Windows shim for the POSIX termios module."

    tty = types.ModuleType("tty")
    tty.setraw = _setraw
    tty.setcbreak = _setcbreak
    tty.__doc__ = "colabapi Windows shim for the POSIX tty module."

    # setdefault semantics: never clobber a real module if one somehow exists.
    sys.modules.setdefault("termios", termios)
    sys.modules.setdefault("tty", tty)

    _patch_signal()
    enable_vt_mode()
    _installed = True
    return True
