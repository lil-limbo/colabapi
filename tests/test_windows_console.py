"""Drive a REAL Windows console: flip raw mode, inject real keys, read them back.

Everything else we can test off-Windows is reasoning about an API contract. This
is the test that actually exercises it, and it is the difference between "the
Win32 docs say this should work" and "this works".

The trick that makes it possible in CI: a GitHub Actions runner is a service, so
its stdio is redirected to pipes and there is no console attached -- which is
exactly why the console code paths normally go untested. But Windows lets a
process *create* a console for itself (AllocConsole) and open its input and
output buffers directly by the special filenames CONIN$ / CONOUT$. Once we hold
a genuine console input handle we can:

  * call GetConsoleMode/SetConsoleMode against it for real, so `tty.setraw()` and
    the tcgetattr/tcsetattr save-restore pair are executed, not simulated; and
  * push synthetic keystrokes into the console's input queue with
    WriteConsoleInput -- arrow keys, Ctrl+C, non-ASCII characters -- and assert
    on the exact bytes colabapi hands to the remote shell.

That last part matters most. The arrow-key path is the one place where the
console API is genuinely counter-intuitive: ENABLE_VIRTUAL_TERMINAL_INPUT does
NOT make msvcrt return ANSI sequences (it only affects ReadFile/ReadConsole
consumers), so msvcrt still yields legacy '\\xe0' + scan-code pairs and we have
to translate them ourselves. If that translation is wrong, arrows arrive at the
remote shell as garbage. This test presses Up and checks that "\\x1b[A" comes
out.

Skips cleanly (exit 0) on Linux/macOS, where it verifies the pure translation
table only.

Run:  python tests/test_windows_console.py
"""

from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def finish() -> None:
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("ALL PASS")
    sys.exit(0)


# --------------------------------------------------------------------------- #
# The key table is a pure function, so it is worth checking everywhere.
# --------------------------------------------------------------------------- #
from colabapi.terminal import translate_windows_key  # noqa: E402

for prefix in ("\x00", "\xe0"):
    check(f"Up arrow ({prefix!r} H) -> ESC[A",
          translate_windows_key(prefix, "H") == "\x1b[A")
check("Down/Left/Right map correctly",
      (translate_windows_key("\xe0", "P"),
       translate_windows_key("\xe0", "K"),
       translate_windows_key("\xe0", "M")) == ("\x1b[B", "\x1b[D", "\x1b[C"))
check("Delete -> ESC[3~", translate_windows_key("\xe0", "S") == "\x1b[3~")
check("Unknown scan code is dropped, not leaked",
      translate_windows_key("\xe0", "\x99") == "")
check("A normal char is not treated as a prefix",
      translate_windows_key("a", "H") == "")

if not IS_WINDOWS:
    print("\n(Not Windows: the console-injection tests below need a real console.)")
    finish()


# --------------------------------------------------------------------------- #
# From here on: a real Windows console.
# --------------------------------------------------------------------------- #
import ctypes  # noqa: E402
from ctypes import wintypes  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11

ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

KEY_EVENT = 0x0001
LEFT_CTRL_PRESSED = 0x0008

k32.AllocConsole.restype = wintypes.BOOL
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                            wintypes.HANDLE]
k32.CreateFileW.restype = wintypes.HANDLE
k32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
k32.SetStdHandle.restype = wintypes.BOOL
k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
k32.GetConsoleMode.restype = wintypes.BOOL
k32.FlushConsoleInputBuffer.argtypes = [wintypes.HANDLE]
k32.FlushConsoleInputBuffer.restype = wintypes.BOOL


class _Char(ctypes.Union):
    _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", _Char),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _Event(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _Event)]


k32.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(INPUT_RECORD),
                                   wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
k32.WriteConsoleInputW.restype = wintypes.BOOL

# --- give this process a real console -------------------------------------- #
# Under CI there is none. If one already exists AllocConsole fails harmlessly and
# we just use it. Note this does NOT disturb Python's sys.stdout: that is bound
# to file descriptor 1, which still points at the pipe, so our PASS/FAIL lines
# still reach the workflow log rather than vanishing into the new console.
k32.AllocConsole()

conin = k32.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE,
                        FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None, OPEN_EXISTING, 0, None)
conout = k32.CreateFileW("CONOUT$", GENERIC_READ | GENERIC_WRITE,
                         FILE_SHARE_READ | FILE_SHARE_WRITE,
                         None, OPEN_EXISTING, 0, None)

check("opened a real console (CONIN$/CONOUT$)",
      conin not in (0, INVALID_HANDLE_VALUE) and conout not in (0, INVALID_HANDLE_VALUE),
      f"conin={conin} conout={conout}")
if failures:
    finish()

# Point the standard handles at it, because _winshim resolves the console through
# GetStdHandle -- this is what makes it operate on our console rather than a pipe.
k32.SetStdHandle(STD_INPUT_HANDLE, conin)
k32.SetStdHandle(STD_OUTPUT_HANDLE, conout)


def console_mode() -> int:
    mode = wintypes.DWORD()
    k32.GetConsoleMode(conin, ctypes.byref(mode))
    return mode.value


# --------------------------------------------------------------------------- #
# 1. Raw mode must really flip the console bits.
# --------------------------------------------------------------------------- #
from colabapi import _winshim  # noqa: E402

_winshim.install()
import termios  # noqa: E402  (the shim's, not POSIX's)
import tty  # noqa: E402

before = console_mode()
saved = termios.tcgetattr(0)
tty.setraw(0, termios.TCSANOW)
raw = console_mode()

check("raw mode disables line buffering (ENABLE_LINE_INPUT off)",
      not (raw & ENABLE_LINE_INPUT), f"mode=0x{raw:04x}")
check("raw mode disables local echo (ENABLE_ECHO_INPUT off)",
      not (raw & ENABLE_ECHO_INPUT))
check("raw mode lets Ctrl+C through to the remote (ENABLE_PROCESSED_INPUT off)",
      not (raw & ENABLE_PROCESSED_INPUT))
check("raw mode enables VT input (ENABLE_VIRTUAL_TERMINAL_INPUT on)",
      bool(raw & ENABLE_VIRTUAL_TERMINAL_INPUT))

termios.tcsetattr(0, termios.TCSANOW, saved)
check("tcsetattr restores the console exactly (no wrecked terminal on exit)",
      console_mode() == before, f"before=0x{before:04x} after=0x{console_mode():04x}")

# Back to raw for the input tests: this is the state a real shell session runs in.
tty.setraw(0, termios.TCSANOW)


# --------------------------------------------------------------------------- #
# 2. Inject real keystrokes and read them back through colabapi's reader.
# --------------------------------------------------------------------------- #
def press(char: str = "\0", vk: int = 0, scan: int = 0, ctrl: bool = False) -> None:
    """Push one key-down event into the console's input queue."""
    rec = INPUT_RECORD()
    rec.EventType = KEY_EVENT
    ke = rec.Event.KeyEvent
    ke.bKeyDown = True
    ke.wRepeatCount = 1
    ke.wVirtualKeyCode = vk
    ke.wVirtualScanCode = scan
    ke.uChar.UnicodeChar = char
    ke.dwControlKeyState = LEFT_CTRL_PRESSED if ctrl else 0
    written = wintypes.DWORD()
    ok = k32.WriteConsoleInputW(conin, ctypes.byref(rec), 1, ctypes.byref(written))
    if not ok or written.value != 1:
        raise OSError(f"WriteConsoleInput failed: {ctypes.get_last_error()}")


from colabapi.terminal import HardenedConsole  # noqa: E402

reader = HardenedConsole("t", quiet=True)


def read_key(timeout: float = 2.0) -> bytes:
    """Read one translated keystroke exactly as a live session would."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        got = reader._read_char()
        if got:
            return got
    return b""


k32.FlushConsoleInputBuffer(conin)

# A plain letter.
press("a", vk=0x41, scan=0x1E)
check("typing 'a' sends b'a'", read_key() == b"a")

# Enter.
press("\r", vk=0x0D, scan=0x1C)
check("Enter sends CR", read_key() == b"\r")

# Ctrl+C must reach the REMOTE process as 0x03, not raise KeyboardInterrupt here.
press("\x03", vk=0x43, scan=0x2E, ctrl=True)
check("Ctrl+C is forwarded as 0x03 (interrupts the remote job, not colabapi)",
      read_key() == b"\x03")

# The arrow keys -- the whole reason the translation table exists.
# A real Up press carries no character, only VK_UP (0x26) / scan 0x48; the CRT
# surfaces that to msvcrt as the legacy '\xe0' 'H' pair.
press("\0", vk=0x26, scan=0x48)
up = read_key()
check("Up arrow arrives as ESC[A, not raw scan codes", up == b"\x1b[A", repr(up))

press("\0", vk=0x28, scan=0x50)
check("Down arrow arrives as ESC[B", read_key() == b"\x1b[B")

press("\0", vk=0x27, scan=0x4D)
check("Right arrow arrives as ESC[C", read_key() == b"\x1b[C")

press("\0", vk=0x25, scan=0x4B)
check("Left arrow arrives as ESC[D", read_key() == b"\x1b[D")

# Non-ASCII: getch would have mangled this through the OEM code page; getwch
# gives us the real character, which must reach the shell as UTF-8.
press("é", vk=0, scan=0)
got = read_key()
check("non-ASCII 'e-acute' survives as UTF-8", got == "é".encode("utf-8"), repr(got))

# The local detach key must be seen as Ctrl+] (0x1d) so `_read_stdin` can act on it.
press("\x1d", vk=0xDD, scan=0x1B, ctrl=True)
check("Ctrl+] (detach) is delivered as 0x1d", read_key() == b"\x1d")

# --------------------------------------------------------------------------- #
# 3. Always hand the console back the way we found it.
# --------------------------------------------------------------------------- #
termios.tcsetattr(0, termios.TCSANOW, saved)
check("console restored at the end", console_mode() == before)

finish()
