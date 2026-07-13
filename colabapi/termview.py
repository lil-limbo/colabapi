"""A real terminal, inside the window.

The Colab shell is a pty on the far end: it sends raw ANSI, moves the cursor,
repaints regions, and (because `persist.py` puts the shell inside tmux) draws a
full-screen status line. Appending its bytes to a text box would render garbage.
So this is an actual terminal emulator widget -- a `pyte` screen buffer fed the
raw stream, painted into a Tk Text widget, with keystrokes translated back into
the byte sequences a terminal would send.

`pyte` does the VT parsing. Writing that by hand is how you end up with a
terminal that mangles anything more ambitious than `ls`; pyte is a pure-Python
implementation of the same state machine, so it is a dependency worth having.

The widget is a dumb screen: it knows nothing about WebSockets or subprocesses.
Whoever owns it supplies `on_input` (keystrokes leaving) and `on_resize` (the
size changed), and calls `feed()` with bytes arriving. That is what lets the
same widget host a remote Colab shell and a local command (see `gui.py`).
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from queue import Empty, Queue
from typing import Callable, Optional

import pyte

# Repaint at most this often. The screen is redrawn wholesale, so a chatty
# command (`pip install`, a training log) would otherwise repaint per frame and
# starve the UI thread. 25ms is well under what an eye resolves and turns a
# burst of output into one paint.
_REDRAW_MS = 25

DEFAULT_COLS = 80
DEFAULT_ROWS = 24

# A dark terminal inside a light window: this pane is a terminal, and reading
# ANSI colour on white is miserable -- most programs assume a dark background.
BG = "#12141a"
FG = "#e6e6e6"
CURSOR = "#57d9a3"

# pyte reports colours by name (its 8/16-colour table) or as a bare 6-digit hex
# string for 256/true-colour. Names map here; hex is passed through.
_COLOURS = {
    "black": "#2b2f38", "red": "#e35d6a", "green": "#57d9a3", "brown": "#d8b46a",
    "yellow": "#d8b46a", "blue": "#6aa9f7", "magenta": "#c98bdb", "cyan": "#5bc6d0",
    "white": "#e6e6e6",
    "brightblack": "#5c6370", "brightred": "#ff7b86", "brightgreen": "#7de6b8",
    "brightbrown": "#f0cd8a", "brightyellow": "#f0cd8a", "brightblue": "#8fc1ff",
    "brightmagenta": "#dda6ec", "brightcyan": "#7fdce5", "brightwhite": "#ffffff",
}


def _colour(name: str, default: str) -> str:
    if not name or name == "default":
        return default
    if name in _COLOURS:
        return _COLOURS[name]
    if len(name) == 6:
        try:
            int(name, 16)
            return "#" + name
        except ValueError:
            pass
    return default


class TerminalView(tk.Frame):
    """A VT screen the user can type into."""

    def __init__(self, parent, on_input: Optional[Callable[[str], None]] = None,
                 on_resize: Optional[Callable[[int, int], None]] = None,
                 font_size: int = 10, **kw):
        super().__init__(parent, bg=BG, **kw)
        self._on_input = on_input
        self._on_resize = on_resize
        self._inbox: Queue = Queue()
        self._timer = None
        self._tags: set[str] = set()

        self.font = tkfont.Font(family=_mono_family(), size=font_size)
        self._cw = max(self.font.measure("M"), 1)
        self._ch = max(self.font.metrics("linespace"), 1)

        self.screen = pyte.Screen(DEFAULT_COLS, DEFAULT_ROWS)
        self.stream = pyte.Stream(self.screen)

        self.text = tk.Text(
            self, bg=BG, fg=FG, font=self.font, relief="flat", borderwidth=0,
            highlightthickness=0, padx=8, pady=6, wrap="none",
            insertbackground=BG,          # no Tk caret; the VT cursor is drawn
            state="disabled", cursor="xterm", takefocus=True,
        )
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("cursor", background=CURSOR, foreground=BG)

        # Keystrokes must reach the runtime, not Tk's own editing bindings, so
        # every handler ends with "break". Focus follows the click, as in any
        # terminal.
        self.text.bind("<Key>", self._on_key)
        self.text.bind("<Button-1>", lambda _e: (self.text.focus_set(), "break")[1])
        self.text.bind("<Configure>", self._on_configure)

        self._drain()      # the one repaint timer, owned by the UI thread

    # -- output --------------------------------------------------------------
    def feed(self, data: str) -> None:
        """Feed raw terminal output in. Safe to call from any thread.

        Output arrives on someone else's thread -- the WebSocket reader, the pty
        reader -- and Tk may only be touched from the thread running its main
        loop. Even `after()` is unsafe from outside it: it registers a Tcl
        command, and doing that concurrently raises "main thread is not in main
        loop" or, worse, corrupts the interpreter. So nothing here touches Tk at
        all: bytes go into a queue, and the UI thread drains it on its own clock
        (`_drain`, below).
        """
        self._inbox.put(data)

    def _drain(self) -> None:
        """UI thread: absorb whatever arrived and repaint at most once."""
        if not self.winfo_exists():
            return
        got = False
        while True:
            try:
                data = self._inbox.get_nowait()
            except Empty:
                break
            got = True
            try:
                self.stream.feed(data)
            except Exception:  # noqa: BLE001 -- a malformed sequence must not kill the UI
                pass
        if got:
            self._redraw()
        # One timer for the widget's whole life, owned by the UI thread. A burst
        # of output therefore costs one repaint per tick, not one per chunk.
        self._timer = self.after(_REDRAW_MS, self._drain)

    def write_note(self, message: str) -> None:
        """A colabapi status line, printed into the screen like a terminal would."""
        self.feed(f"\r\n\x1b[36m[colabapi]\x1b[0m {message}\r\n")

    def _redraw(self) -> None:
        if not self.winfo_exists():
            return
        t = self.text
        t.configure(state="normal")
        # Repainting the whole screen each frame is what a terminal does, and it
        # keeps this honest: the buffer is the single source of truth, so there
        # is no way for the view to drift out of sync with it.
        yview = t.yview()
        t.delete("1.0", "end")
        buf = self.screen.buffer
        for y in range(self.screen.lines):
            row = buf[y]
            # Group runs of identical style: one insert per run instead of one
            # per cell turns ~2000 Tk calls per frame into a few dozen.
            run: list[str] = []
            style: Optional[tuple] = None
            for x in range(self.screen.columns):
                ch = row[x]
                st = (ch.fg, ch.bg, ch.bold, ch.reverse)
                if st != style and run:
                    t.insert("end", "".join(run), self._tag(style))
                    run = []
                style = st
                run.append(ch.data or " ")
            if run:
                t.insert("end", "".join(run), self._tag(style))
            if y != self.screen.lines - 1:
                t.insert("end", "\n")

        if not self.screen.cursor.hidden:
            cy, cx = self.screen.cursor.y, self.screen.cursor.x
            if 0 <= cy < self.screen.lines and 0 <= cx < self.screen.columns:
                start = f"{cy + 1}.{cx}"
                t.tag_add("cursor", start, f"{start} +1c")
        t.configure(state="disabled")
        t.yview_moveto(yview[0] if yview[0] else 0.0)

    def _tag(self, style: Optional[tuple]) -> str:
        if style is None:
            return ""
        fg, bg, bold, reverse = style
        name = f"s_{fg}_{bg}_{int(bool(bold))}_{int(bool(reverse))}"
        if name not in self._tags:
            f = _colour(fg, FG)
            b = _colour(bg, BG)
            if reverse:
                f, b = b, f
            font = self.font
            if bold:
                font = tkfont.Font(font=self.font)
                font.configure(weight="bold")
            self.text.tag_configure(name, foreground=f, background=b, font=font)
            self._tags.add(name)
        return name

    # -- input ---------------------------------------------------------------
    def set_handlers(self, on_input: Optional[Callable[[str], None]],
                     on_resize: Optional[Callable[[int, int], None]] = None) -> None:
        """Point the keyboard at a backend (a remote shell, a local command, or
        None -- in which case typing goes nowhere, which is the honest behaviour
        for a screen with nothing attached)."""
        self._on_input = on_input
        self._on_resize = on_resize

    def _on_key(self, event) -> str:
        seq = _key_to_bytes(event)
        if seq and self._on_input is not None:
            self._on_input(seq)
        return "break"      # never let Tk edit the widget itself

    # -- geometry ------------------------------------------------------------
    def _on_configure(self, _event=None) -> None:
        cols = max((self.text.winfo_width() - 16) // self._cw, 20)
        rows = max((self.text.winfo_height() - 12) // self._ch, 5)
        if cols == self.screen.columns and rows == self.screen.lines:
            return
        self.screen.resize(rows, cols)
        self._redraw()
        if self._on_resize:
            self._on_resize(cols, rows)

    def size(self) -> tuple:
        return self.screen.columns, self.screen.lines

    def reset(self) -> None:
        self.screen.reset()
        self._redraw()


def _mono_family() -> str:
    """The first monospace family actually present; Tk silently substitutes
    something proportional otherwise, which would misalign every column."""
    try:
        available = set(tkfont.families())
    except tk.TclError:
        return "Courier"
    for name in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Liberation Mono",
                 "Ubuntu Mono", "Menlo", "Monaco", "Courier New"):
        if name in available:
            return name
    return "Courier"


# Tk keysym -> what a terminal actually puts on the wire.
_KEYS = {
    "Return": "\r", "KP_Enter": "\r", "BackSpace": "\x7f", "Tab": "\t",
    "Escape": "\x1b", "Up": "\x1b[A", "Down": "\x1b[B", "Right": "\x1b[C",
    "Left": "\x1b[D", "Home": "\x1b[H", "End": "\x1b[F", "Prior": "\x1b[5~",
    "Next": "\x1b[6~", "Delete": "\x1b[3~", "Insert": "\x1b[2~",
    "F1": "\x1bOP", "F2": "\x1bOQ", "F3": "\x1bOR", "F4": "\x1bOS",
    "F5": "\x1b[15~", "F6": "\x1b[17~", "F7": "\x1b[18~", "F8": "\x1b[19~",
    "F9": "\x1b[20~", "F10": "\x1b[21~", "F11": "\x1b[23~", "F12": "\x1b[24~",
}

# Modifier bits Tk reports in event.state.
_CTRL = 0x0004


def _key_to_bytes(event) -> str:
    """Translate one keypress into the sequence a terminal would send.

    Order matters: the named keys first (Return must be \\r, not the "\\r" Tk
    also puts in event.char for it), then whatever printable character Tk
    decoded. Tk already folds Ctrl+letter into a control character in
    `event.char`, so that path needs no table -- except Ctrl+Space, which it
    reports as an empty char and which a shell expects as NUL.
    """
    keysym = event.keysym
    if keysym in _KEYS:
        return _KEYS[keysym]
    if keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L",
                  "Alt_R", "Super_L", "Super_R", "Caps_Lock", "Num_Lock"):
        return ""
    ch = event.char
    if ch:
        return ch
    if (event.state & _CTRL) and keysym == "space":
        return "\x00"
    return ""
