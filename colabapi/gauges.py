"""Live CPU / RAM / GPU / VRAM graphs for the selected session.

What the numbers are: they are read from *inside* the Colab runtime (a small
Python snippet run over the tunnel -- see `monitor.read_stats`), so they describe
the VM you are paying attention to, not the laptop the window is on.

Two pieces, kept apart on purpose:

  * `Sampler` -- a background thread that polls one session and hands each
    reading back on the UI thread. It owns the cadence and the failure handling,
    and it can be pointed at a different session (or none) at any time.
  * `Graphs` -- a dumb Tk widget. Given a reading it draws; given nothing it says
    so. It never talks to Colab.

Sampling costs a `colab exec` round trip, so the cadence is seconds, not
milliseconds, and a sample is skipped entirely while the previous one is still in
flight -- a slow runtime must never queue up a backlog of probes.
"""

from __future__ import annotations

import threading
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from typing import Callable, Optional

from . import monitor

# At one reading a second, this is the last three minutes -- enough to watch a
# training step spike and settle, without holding data nobody looks at.
HISTORY = 180

# The palette is shared with the rest of the window (gui.py) but redeclared as
# graph roles, so a colour change here cannot silently restyle a button.
INK = "#111111"
MUTED = "#6b6b6b"
GRID = "#ececec"
CARD = "#fafafa"
BORDER = "#e0e0e0"

CPU_COLOUR = "#3b82f6"
RAM_COLOUR = "#8b5cf6"
GPU_COLOUR = "#10b981"
VRAM_COLOUR = "#f59e0b"
DEAD = "#c9c9c9"


class Sampler:
    """Polls one session's vitals on a background thread."""

    def __init__(self, run_remote: Callable[[str, str], str],
                 on_sample: Callable[[Optional[dict], str], None],
                 interval: float = 1.0):
        # run_remote(session_name, code) -> stdout. Injected rather than built
        # here so the GUI can hand in the same ColabCLI the buttons drive.
        self._run_remote = run_remote
        self._on_sample = on_sample
        # One reading a second. A reading costs a round trip to the runtime, and
        # if that trip takes longer than a second the next tick is skipped rather
        # than queued (see `_busy` below) -- so the monitor runs as fast as the
        # link allows and never builds a backlog of stale probes.
        self.interval = max(interval, 1.0)
        self._session: Optional[str] = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="colabapi-sampler",
                                            daemon=True)
            self._thread.start()

    def watch(self, session: Optional[str]) -> None:
        """Point the sampler at a session (or None to idle). Takes effect now."""
        with self._lock:
            if session == self._session:
                return
            self._session = session
        self._wake.set()          # don't make the user wait out the current sleep

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                session = self._session
            if session is None:
                self._on_sample(None, "")
            elif not self._busy:
                self._busy = True
                try:
                    stats = monitor.read_stats(lambda code: self._run_remote(session, code))
                    self._on_sample(stats, "")
                except Exception as exc:  # noqa: BLE001 -- a dead runtime is news, not a crash
                    self._on_sample(None, _reason(exc))
                finally:
                    self._busy = False
            self._wake.wait(self.interval)
            self._wake.clear()


def _reason(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    first = text[0] if text else exc.__class__.__name__
    return first[:120]


class _Sparkline(tk.Canvas):
    """One metric: a title, a current value, and its recent history."""

    def __init__(self, parent, label: str, colour: str, height: int = 74, **kw):
        super().__init__(parent, height=height, bg=CARD, highlightthickness=1,
                         highlightbackground=BORDER, bd=0, **kw)
        self.label = label
        self.colour = colour
        self.history: deque = deque(maxlen=HISTORY)
        self._value = ""
        self._detail = ""
        self._live = True
        self._title_font = tkfont.Font(family=_ui_family(), size=8, weight="bold")
        self._value_font = tkfont.Font(family=_ui_family(), size=13, weight="bold")
        self._detail_font = tkfont.Font(family=_ui_family(), size=8)
        self.bind("<Configure>", lambda _e: self._draw())

    def push(self, pct: Optional[float], value: str, detail: str = "") -> None:
        if pct is not None:
            self.history.append(max(0.0, min(float(pct), 100.0)))
        self._value, self._detail = value, detail
        self._live = pct is not None
        self._draw()

    def clear(self, value: str = "—") -> None:
        self.history.clear()
        self._value, self._detail, self._live = value, "", False
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        colour = self.colour if self._live else DEAD

        self.create_text(10, 9, text=self.label.upper(), anchor="nw",
                         fill=MUTED, font=self._title_font)
        self.create_text(10, 22, text=self._value or "—", anchor="nw",
                         fill=INK if self._live else MUTED, font=self._value_font)
        if self._detail:
            self.create_text(w - 10, 12, text=self._detail, anchor="ne",
                             fill=MUTED, font=self._detail_font)

        # The plot occupies the lower band; the reading stays legible above it.
        top, bottom = h * 0.55, h - 6
        span = max(bottom - top, 1)
        self.create_line(8, bottom, w - 8, bottom, fill=GRID)
        n = len(self.history)
        if n < 2:
            if self._live:
                self.create_text(w // 2, (top + bottom) / 2, text="collecting…",
                                 fill=MUTED, font=self._detail_font)
            return

        # Always plot against a full 0-100 scale rather than autoscaling: a CPU
        # idling at 2% must *look* idle, and an autoscaled axis would draw it as
        # a dramatic mountain range.
        left, right = 8, w - 8
        step = (right - left) / max(n - 1, 1)
        pts = [(left + i * step, bottom - (v / 100.0) * span)
               for i, v in enumerate(self.history)]
        area = [(left, bottom)] + pts + [(pts[-1][0], bottom)]
        self.create_polygon([c for p in area for c in p],
                            fill=_tint(colour), outline="")
        self.create_line([c for p in pts for c in p], fill=colour, width=2,
                         smooth=True, capstyle="round", joinstyle="round")
        x, y = pts[-1]
        self.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, fill=colour, outline="")


def _tint(hex_colour: str) -> str:
    """A pale wash of the line colour for the area under it. Tk has no alpha, so
    the colour is mixed toward the card background instead."""
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    br, bg_, bb = (int(CARD[i:i + 2], 16) for i in (1, 3, 5))
    mix = lambda c, base: int(base + (c - base) * 0.16)  # noqa: E731
    return f"#{mix(r, br):02x}{mix(g, bg_):02x}{mix(b, bb):02x}"


class Graphs(tk.Frame):
    """The row of live graphs across the top of the window."""

    def __init__(self, parent, bg: str = "#ffffff", **kw):
        super().__init__(parent, bg=bg, **kw)
        self.cpu = _Sparkline(self, "CPU", CPU_COLOUR)
        self.ram = _Sparkline(self, "RAM", RAM_COLOUR)
        self.gpu = _Sparkline(self, "GPU", GPU_COLOUR)
        self.vram = _Sparkline(self, "VRAM", VRAM_COLOUR)
        for i, card in enumerate((self.cpu, self.ram, self.gpu, self.vram)):
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0))
            self.columnconfigure(i, weight=1, uniform="g")

    def update_from(self, stats: Optional[dict], reason: str = "") -> None:
        """Paint one reading. `None` means there is nothing to read, and the
        reason (if any) is shown rather than swallowed."""
        if not stats:
            for card in (self.cpu, self.ram, self.gpu, self.vram):
                card.clear()
            self.cpu._detail = reason[:40] if reason else ""
            self.cpu._draw()
            return

        cpu = stats.get("cpu") or 0.0
        self.cpu.push(cpu, f"{cpu:.0f}%")

        used, total = stats.get("mem_used") or 0.0, stats.get("mem_total") or 0.0
        pct = (used / total * 100) if total else 0.0
        self.ram.push(pct, f"{pct:.0f}%", f"{used/1024:.1f} / {total/1024:.1f} GiB")

        gpus = stats.get("gpus") or []
        if not gpus:
            # A CPU-only runtime is a normal thing to have, not a failure: say
            # what it is instead of drawing two dead graphs.
            self.gpu.clear("—")
            self.gpu._detail = "no GPU"
            self.gpu._draw()
            self.vram.clear("—")
            self.vram._detail = "CPU-only runtime"
            self.vram._draw()
            return

        g = gpus[0]
        self.gpu.push(g["util"], f"{g['util']:.0f}%",
                      f"{g['name']}  {g['temp']:.0f}°C")
        vused, vtotal = g["mem_used"], g["mem_total"]
        vpct = (vused / vtotal * 100) if vtotal else 0.0
        self.vram.push(vpct, f"{vpct:.0f}%",
                       f"{vused/1024:.1f} / {vtotal/1024:.1f} GiB")


def _ui_family() -> str:
    try:
        families = set(tkfont.families())
    except tk.TclError:
        return "Helvetica"
    for name in ("Segoe UI", "Inter", "DejaVu Sans", "Cantarell", "Helvetica"):
        if name in families:
            return name
    return "Helvetica"
