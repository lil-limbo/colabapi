"""Tiny interactive terminal helpers (no extra dependencies).

The arrow-key `select` menu is used wherever colabapi asks the user to pick a
session (shell / stop / monitor). On POSIX it uses raw terminal mode via
termios; on Windows it reads keys through msvcrt (the termios shim in `_winshim`
handles the mode switching, but key *decoding* is different enough -- special
keys arrive as 0x00/0xE0-prefixed scan codes, not ANSI sequences -- that the
reader needs its own branch). When stdin is not a TTY (e.g. run from a service)
it degrades to returning the first option, so non-interactive callers still
work.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional, Sequence, TypeVar

from .platform import IS_WINDOWS

T = TypeVar("T")

_ESC = "\x1b"


def _read_key_windows() -> str:
    """One logical keypress via the Windows console."""
    import msvcrt

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):  # special key: prefix + scan code
        code = msvcrt.getwch()
        return {"H": "up", "P": "down"}.get(code, "esc")
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":  # Ctrl-C
        raise KeyboardInterrupt
    if ch == _ESC:
        return "esc"
    return _normalize(ch)


def _read_key_posix() -> str:
    """Read one logical keypress in raw mode and normalise it to a name."""
    import select

    ch = sys.stdin.read(1)
    if ch == _ESC:
        # Arrow keys arrive as ESC [ A/B/C/D. A bare Esc press sends only the
        # one byte, so the follow-up read must not block waiting for two more
        # characters that will never come.
        if not select.select([sys.stdin], [], [], 0.05)[0]:
            return "esc"
        nxt = sys.stdin.read(2)
        return {"[A": "up", "[B": "down"}.get(nxt, "esc")
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":  # Ctrl-C
        raise KeyboardInterrupt
    return _normalize(ch)


def _normalize(ch: str) -> str:
    if ch in ("k", "K"):
        return "up"
    if ch in ("j", "J"):
        return "down"
    if ch in ("q", "Q"):
        return "quit"
    return ch


def select(options: Sequence[T],
           title: str = "Select",
           to_label: Callable[[T], str] = str,
           footer: str = "↑/↓ move · Enter select · q cancel") -> Optional[T]:
    """Interactive single-choice menu. Returns the chosen item, or None if cancelled.

    With zero options returns None; with exactly one option (or no TTY) returns it
    immediately without prompting.
    """
    n = len(options)
    if n == 0:
        return None
    if n == 1 or not sys.stdin.isatty() or not sys.stdout.isatty():
        return options[0]

    idx = 0
    out = sys.stdout
    lines = 0

    def draw() -> None:
        nonlocal lines
        buf = [f"\x1b[1;36m{title}\x1b[0m"]
        for i, opt in enumerate(options):
            label = to_label(opt)
            if i == idx:
                buf.append(f"\x1b[1;32m❯ {label}\x1b[0m")
            else:
                buf.append(f"  {label}")
        buf.append(f"\x1b[2m{footer}\x1b[0m")
        out.write("\r\n".join(buf))
        out.flush()
        lines = len(buf)

    if IS_WINDOWS:
        # Registers the termios/tty stand-ins (and switches the console to VT
        # output so the ANSI drawing below renders) before we import them.
        from . import _winshim

        _winshim.install()
    import termios
    import tty

    read_key = _read_key_windows if IS_WINDOWS else _read_key_posix

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    chosen: Optional[T] = None
    try:
        tty.setraw(fd)
        draw()
        while True:
            key = read_key()
            if key == "up":
                idx = (idx - 1) % n
            elif key == "down":
                idx = (idx + 1) % n
            elif key == "enter":
                chosen = options[idx]
                break
            elif key in ("quit", "esc"):
                chosen = None
                break
            else:
                continue
            # Move cursor to the top of the block and repaint.
            out.write(f"\r\x1b[{lines - 1}A\x1b[J")
            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        out.write("\r\n")
        out.flush()
    return chosen
