"""Run a local command on a pty, so the window can host it.

The reason `gui.py` used to throw Login, Run and Monitor into a *new terminal
window* is that they are interactive: Google's sign-in prints a URL and waits,
`run` asks which runtime and what to name it, the monitor repaints. A captured
subprocess cannot do any of that -- with a pipe on stdout, the child sees "not a
terminal", Rich stops rendering, prompts never flush, and there is nowhere to
type the answer.

A pty fixes it at the root: the child gets a real terminal (because it is one),
behaves exactly as it does in a shell, and its bytes land in the same
`TerminalView` the remote shell uses. Nothing spawns a window.

POSIX only, deliberately. Windows has no `pty`; the equivalent is ConPTY through
a third-party wheel, and rather than pretend, `available()` returns False there
and the window keeps its existing new-console path (see `gui.py`). Linux and
macOS -- where colabapi actually runs headless servers -- get the real thing.
"""

from __future__ import annotations

import codecs
import os
import shutil
import signal
import struct
import subprocess
import sys
import threading
from typing import Callable, Optional, Sequence

from .platform import IS_WINDOWS


def available() -> bool:
    """True when a pty can be allocated (i.e. not Windows)."""
    if IS_WINDOWS:
        return False
    try:
        import pty  # noqa: F401
        import fcntl  # noqa: F401
        import termios  # noqa: F401
    except ImportError:
        return False
    return True


def cli_argv() -> list:
    """The command that re-invokes colabapi itself.

    Prefers the installed script (what the user's PATH knows), falls back to
    `python -m colabapi`, which still works when the window was started from a
    Start-menu shortcut whose argv[0] is pythonw.
    """
    argv0 = sys.argv[0] or ""
    if (os.path.basename(argv0).lower().startswith("colabapi")
            and os.path.isfile(argv0) and os.access(argv0, os.X_OK)):
        return [argv0]
    found = shutil.which("colabapi")
    if found:
        return [found]
    return [sys.executable, "-m", "colabapi"]


class LocalCommand:
    """One command running on its own pty, streaming into a sink."""

    def __init__(self, argv: Sequence[str], sink: Callable[[str], None],
                 on_exit: Optional[Callable[[int], None]] = None,
                 cols: int = 80, rows: int = 24):
        self.argv = list(argv)
        self._sink = sink
        self._on_exit = on_exit
        self._cols, self._rows = cols, rows
        self._master: Optional[int] = None
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        import pty

        master, slave = pty.openpty()
        self._master = master
        _set_size(master, self._cols, self._rows)

        env = os.environ.copy()
        # Without TERM the child assumes a dumb terminal and drops colour and
        # cursor addressing; with it, what pyte receives is what a terminal
        # receives. COLUMNS/LINES are set too because Rich reads them before it
        # falls back to ioctl.
        env["TERM"] = "xterm-256color"
        env["COLUMNS"], env["LINES"] = str(self._cols), str(self._rows)
        env.pop("NO_COLOR", None)
        # Deliberately NOT FORCE_COLOR: the pty already makes isatty() true, so
        # Rich colours the child anyway -- and FORCE_COLOR is inherited by the
        # child's own children. `colabapi doctor` captures `colab new --help`
        # through a pipe and looks for "--gpu" in it; with colour forced, Rich
        # writes escape sequences *inside* that token and the check reports flag
        # drift that does not exist. Forcing colour on a pipe corrupts text that
        # something downstream is parsing.
        env.pop("FORCE_COLOR", None)

        self._proc = subprocess.Popen(
            self.argv, stdin=slave, stdout=slave, stderr=slave,
            env=env, close_fds=True,
            # Its own session, so the child is the pty's controlling process and
            # Ctrl+C typed in the widget reaches it (and only it) as SIGINT.
            start_new_session=True,
        )
        os.close(slave)   # the child holds the only copy it needs
        threading.Thread(target=self._pump, name="colabapi-pty", daemon=True).start()

    def _pump(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        assert self._master is not None
        while not self._stop.is_set():
            try:
                chunk = os.read(self._master, 4096)
            except OSError:
                break          # the child closed the pty: it exited
            if not chunk:
                break
            text = decoder.decode(chunk)
            if text:
                self._sink(text)
        code = self._reap()
        if self._on_exit is not None:
            self._on_exit(code)

    def _reap(self) -> int:
        if self._proc is None:
            return 0
        try:
            return self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # It stopped writing but has not exited (a prompt waiting on a child
            # of its own). Don't hang the reader thread on it.
            return -1

    # -- I/O -----------------------------------------------------------------
    def send(self, text: str) -> None:
        if self._master is None or self._stop.is_set():
            return
        try:
            os.write(self._master, text.encode("utf-8"))
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        if self._master is not None:
            _set_size(self._master, cols, rows)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        """Stop the command and release the pty."""
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                # The whole process group: `colabapi run` shells out to `colab`,
                # and killing only the parent would orphan it still holding the
                # pty.
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    proc.terminate()
                except OSError:
                    pass
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = None


def _set_size(fd: int, cols: int, rows: int) -> None:
    import fcntl
    import termios

    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass          # a resize that fails is cosmetic, never fatal
