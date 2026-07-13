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

# A light terminal, to match the rest of the window.
BG = "#ffffff"
FG = "#1f2328"
CURSOR = "#1a73e8"
BORDER = "#e0e0e0"

# pyte reports colours by name (its 8/16-colour table) or as a bare 6-digit hex
# string for 256/true-colour.
#
# The names cannot be taken literally on a white background. ANSI "white" means
# "the light end of the palette", which on white is invisible, and the default
# bright colours are washed out to the point of being unreadable. So the table
# maps each name to a colour of the same *hue* darkened enough to read on white,
# and "white"/"brightwhite" become greys rather than disappearing.
_COLOURS = {
    "black": "#24292f", "red": "#cf222e", "green": "#116329", "brown": "#9a6700",
    "yellow": "#9a6700", "blue": "#0550ae", "magenta": "#8250df", "cyan": "#1b7c83",
    "white": "#6e7781",
    "brightblack": "#57606a", "brightred": "#a40e26", "brightgreen": "#1a7f37",
    "brightbrown": "#7d4e00", "brightyellow": "#7d4e00", "brightblue": "#0969da",
    "brightmagenta": "#6639ba", "brightcyan": "#3192aa", "brightwhite": "#24292f",
}

# A 256/true-colour value chosen for a dark terminal can be too pale to read
# here. Rather than pass it through and hope, anything this light is darkened
# until it clears the threshold, so no program can render itself invisible.
_MIN_CONTRAST = 0.62      # relative luminance ceiling for text on white


def _colour(name: str, default: str) -> str:
    if not name or name == "default":
        return default
    if name in _COLOURS:
        return _COLOURS[name]
    if len(name) == 6:
        try:
            r, g, b = (int(name[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return default
        lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
        if lum > _MIN_CONTRAST:
            scale = _MIN_CONTRAST / lum
            r, g, b = (int(c * scale) for c in (r, g, b))
        return f"#{r:02x}{g:02x}{b:02x}"
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

        # A white pane on a white window needs an edge, or the terminal has no
        # visible boundary at all.
        self.text = tk.Text(
            self, bg=BG, fg=FG, font=self.font, relief="flat", borderwidth=0,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER,
            padx=8, pady=6, wrap="none",
            insertbackground=BG,          # no Tk caret; the VT cursor is drawn
            state="disabled", cursor="xterm", takefocus=True,
        )
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("cursor", background=CURSOR, foreground=BG)

        # Keystrokes must reach the runtime, not Tk's own editing bindings, so
        # the key handler ends with "break". The mouse handler must NOT: Tk's
        # default press-drag behaviour is what makes text selectable, and
        # selection is what Ctrl+C copies. Focus follows the click, as in any
        # terminal.
        self.text.bind("<Key>", self._on_key)
        self.text.bind("<Button-1>", lambda _e: self.text.focus_set())
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
        # Clipboard first, the way every modern terminal resolves the clash
        # between "Ctrl+C is copy" and "Ctrl+C interrupts": with text selected
        # it copies, with nothing selected it goes to the remote shell as the
        # interrupt. Ctrl+V always pastes (the remote shell has no use for a
        # raw \x16).
        if event.state & _CTRL:
            key = event.keysym.lower()
            if key == "c" and self._copy_selection():
                return "break"
            if key == "v":
                self._paste()
                return "break"
        seq = _key_to_bytes(event)
        if seq and self._on_input is not None:
            self._on_input(seq)
        return "break"      # never let Tk edit the widget itself

    def _copy_selection(self) -> bool:
        """Copy the mouse selection to the clipboard. False if nothing selected."""
        try:
            selected = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            return False
        if not selected:
            return False
        self.clipboard_clear()
        self.clipboard_append(selected)
        # Drop the selection so the NEXT Ctrl+C is an interrupt again -- without
        # this, a forgotten week-old selection would eat every Ctrl+C.
        self.text.tag_remove("sel", "1.0", "end")
        return True

    def _paste(self) -> None:
        """Type the clipboard into the runtime."""
        try:
            clip = self.clipboard_get()
        except tk.TclError:      # empty clipboard, or non-text content
            return
        if clip and self._on_input is not None:
            # A pty expects Enter as \r; pasted text carries \n (or \r\n).
            self._on_input(clip.replace("\r\n", "\n").replace("\n", "\r"))

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
