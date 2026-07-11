"""Tiny interactive terminal helpers (no extra dependencies).

The arrow-key `select` menu is used wherever colabapi asks the user to pick a
session (shell / stop / monitor). It uses raw terminal mode via termios, which is
fine because the underlying `colab` CLI is Linux/macOS only. When stdin is not a
TTY (e.g. run from a service) it degrades to returning the first option, so
non-interactive callers still work.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional, Sequence, TypeVar

T = TypeVar("T")

_ESC = "\x1b"


def _read_key() -> str:
    """Read one logical keypress in raw mode and normalise it to a name."""
    ch = sys.stdin.read(1)
    if ch == _ESC:
        nxt = sys.stdin.read(2)  # arrow keys arrive as ESC [ A/B/C/D
        return {"[A": "up", "[B": "down"}.get(nxt, "esc")
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":  # Ctrl-C
        raise KeyboardInterrupt
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

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    chosen: Optional[T] = None
    try:
        tty.setraw(fd)
        draw()
        while True:
            key = _read_key()
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
