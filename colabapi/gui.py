"""The colabapi desktop window (`colabapi ui`).

A thin graphical front end over the CLI, for the user who installed colabapi
from the Start menu or an app list and expects a window, not a prompt. It is
deliberately not a re-implementation of anything: every button shells out to
the corresponding `colabapi` subcommand, so the CLI stays the single source of
behaviour and the window can never drift from it.

Two kinds of button, two launch styles:

  * Interactive commands (login, run, shell, monitor) need a real terminal --
    Google's login opens a browser and prints a URL, the shell is a live PTY.
    Those open in a new terminal window (a fresh console on Windows; the
    first terminal emulator found on Linux).
  * Read-only commands (sessions, status, doctor) run captured and print into
    the output pane inside the window. Logout does too, then the signed-in
    line refreshes.

Tkinter is the toolkit because it ships inside Python itself: no new runtime
dependency, works on Windows and Linux alike. The one wrinkle is that Debian
family distros split it into `python3-tk`; the import is therefore wrapped and
surfaced as TkUnavailable so the CLI can print the fix instead of a traceback.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from typing import Callable, Optional

from .platform import IS_WINDOWS


class TkUnavailable(RuntimeError):
    """Raised when Tkinter cannot be imported or no display is reachable."""


def _import_tk():
    try:
        import tkinter as tk
        from tkinter import scrolledtext
    except ImportError as exc:
        raise TkUnavailable(str(exc)) from exc
    return tk, scrolledtext


# --------------------------------------------------------------------------- #
# palette / metrics -- one place, so the window stays coherent
# --------------------------------------------------------------------------- #
BG = "#ffffff"
FG = "#111111"
MUTED = "#6b6b6b"
BTN_BG = "#f4f4f4"
BTN_HOVER = "#e8e8e8"
BORDER = "#e0e0e0"
OUT_BG = "#fafafa"

FONT = "Segoe UI" if IS_WINDOWS else "DejaVu Sans"
MONO = "Consolas" if IS_WINDOWS else "DejaVu Sans Mono"


# --------------------------------------------------------------------------- #
# how to reach the CLI
# --------------------------------------------------------------------------- #
def _cli_command() -> list[str]:
    """The command that runs colabapi, robust to how *this* process started.

    Prefer the installed executable (it is what the user's PATH knows), fall
    back to `python -m colabapi` which always works from inside the package's
    own environment (e.g. launched via pythonw from the Start menu shortcut).
    """
    argv0 = sys.argv[0] or ""
    base = os.path.basename(argv0).lower()
    if base.startswith("colabapi") and os.path.isfile(argv0) and os.access(argv0, os.X_OK):
        return [argv0]
    found = shutil.which("colabapi")
    if found:
        return [found]
    return [sys.executable, "-m", "colabapi"]


def _open_in_terminal(args: list[str]) -> Optional[str]:
    """Run `colabapi <args>` in a NEW terminal window.

    Returns None on success, or a human-readable reason it could not.
    """
    cmd = _cli_command() + args
    try:
        if IS_WINDOWS:
            # cmd /k keeps the window open after the command finishes, so the
            # user can read whatever it printed (a login URL, an error).
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(["cmd.exe", "/k"] + cmd, creationflags=flags)
            return None
        # Debian's alternatives symlink first (it points at whatever the user
        # actually uses), then the common emulators.
        for term, prefix in (
            ("x-terminal-emulator", ["-e"]),
            ("gnome-terminal", ["--"]),
            ("konsole", ["-e"]),
            ("xterm", ["-e"]),
        ):
            path = shutil.which(term)
            if path:
                subprocess.Popen([path] + prefix + cmd)
                return None
        return ("No terminal emulator found (tried x-terminal-emulator, "
                "gnome-terminal, konsole, xterm).\n"
                "Run this in any terminal instead:  colabapi " + " ".join(args))
    except OSError as exc:
        return f"Could not open a terminal: {exc}"


def _run_captured(args: list[str]) -> str:
    """Run `colabapi <args>` with output captured, for the in-window pane."""
    try:
        r = subprocess.run(_cli_command() + args, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Could not run colabapi {' '.join(args)}: {exc}"
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    return out.strip() or f"(colabapi {' '.join(args)} finished with exit code {r.returncode})"


# --------------------------------------------------------------------------- #
# state shown in the header
# --------------------------------------------------------------------------- #
def _signed_in() -> bool:
    """Google's CLI caches its token at this literal path on every platform."""
    return os.path.exists(os.path.expanduser(os.path.join("~", ".config", "colab-cli", "token.json")))


def _active_session_count() -> int:
    try:
        from .config import SessionStore
        return len(SessionStore.load().active())
    except Exception:  # noqa: BLE001 -- a corrupt store must not kill the window
        return 0


def _status_text() -> str:
    signed = "Signed in to Google" if _signed_in() else "Not signed in (use Login)"
    n = _active_session_count()
    sessions = f"{n} active session{'s' if n != 1 else ''}"
    return f"{signed}   ·   {sessions}"


def _logo_path() -> Optional[str]:
    """Filesystem path of the bundled logo (pip installs are plain directories)."""
    try:
        from importlib.resources import files
        p = files("colabapi").joinpath("assets/colabapi.png")
        return str(p) if p.is_file() else None
    except Exception:  # noqa: BLE001 -- a missing asset only costs the icon
        return None


# --------------------------------------------------------------------------- #
# the window
# --------------------------------------------------------------------------- #
def run() -> int:
    """Build and run the window. Returns a process exit code."""
    tk, scrolledtext = _import_tk()

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        # No $DISPLAY (headless box, ssh without -X): a config problem, not a bug.
        raise TkUnavailable(f"no display available ({exc})") from exc

    root.title("colabapi")
    root.configure(bg=BG)
    root.minsize(520, 560)

    # Window / taskbar icon from the bundled logo. PhotoImage reads PNG natively
    # on Tk 8.6+, which is everything Python 3.12+ ships with.
    logo_img = None
    logo = _logo_path()
    if logo:
        try:
            logo_img = tk.PhotoImage(file=logo)
            root.iconphoto(True, logo_img)
        except tk.TclError:
            logo_img = None

    # ---- header: logo + wordmark -------------------------------------------
    header = tk.Frame(root, bg=BG)
    header.pack(fill="x", padx=24, pady=(24, 4))
    if logo_img is not None:
        # 256px source shown at 40px; integer subsampling is all Tk offers.
        small = logo_img.subsample(max(logo_img.width() // 40, 1))
        logo_label = tk.Label(header, image=small, bg=BG)
        logo_label.image = small  # keep a reference or Tk garbage-collects it
        logo_label.pack(side="left", padx=(0, 12))
    tk.Label(header, text="colabapi", bg=BG, fg=FG,
             font=(FONT, 22, "bold")).pack(side="left")

    tk.Label(root, text="Run and keep a Google Colab runtime alive, from your own machine.",
             bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w", padx=24)

    status_var = tk.StringVar(value=_status_text())
    tk.Label(root, textvariable=status_var, bg=BG, fg=MUTED,
             font=(FONT, 10)).pack(anchor="w", padx=24, pady=(8, 16))

    # ---- output pane (created before buttons so callbacks can write to it) --
    out_frame = tk.Frame(root, bg=BG)
    out = scrolledtext.ScrolledText(
        out_frame, height=12, bg=OUT_BG, fg=FG, font=(MONO, 9),
        relief="flat", borderwidth=0, highlightthickness=1,
        highlightbackground=BORDER, highlightcolor=BORDER,
        padx=10, pady=8, state="disabled", wrap="word",
    )
    out.pack(fill="both", expand=True)

    def show_output(title: str, text: str) -> None:
        out.configure(state="normal")
        out.delete("1.0", "end")
        out.insert("1.0", f"$ colabapi {title}\n\n{text}\n")
        out.configure(state="disabled")

    def refresh_status() -> None:
        status_var.set(_status_text())

    # ---- actions ------------------------------------------------------------
    def terminal_action(args: list[str]) -> Callable[[], None]:
        def go() -> None:
            err = _open_in_terminal(args)
            if err:
                show_output(" ".join(args), err)
            else:
                show_output(" ".join(args), "Opened in a new terminal window.")
            # Login / run change the header state once they finish out there;
            # poll a few times so the window catches up without a restart.
            for delay in (3000, 8000, 20000):
                root.after(delay, refresh_status)
        return go

    def captured_action(args: list[str]) -> Callable[[], None]:
        def go() -> None:
            show_output(" ".join(args), "running…")

            def worker() -> None:
                text = _run_captured(args)
                root.after(0, lambda: (show_output(" ".join(args), text), refresh_status()))

            threading.Thread(target=worker, daemon=True).start()
        return go

    actions: list[tuple[str, str, Callable[[], None]]] = [
        ("Login", "Sign in with Google (opens a terminal + browser)", terminal_action(["login"])),
        ("Run", "Allocate a Colab runtime", terminal_action(["run"])),
        ("Shell", "Terminal into the runtime", terminal_action(["shell"])),
        ("Monitor", "Live CPU / GPU / RAM view", terminal_action(["monitor"])),
        ("Sessions", "List active sessions", captured_action(["sessions"])),
        ("Status", "Reachability and time left", captured_action(["status"])),
        ("Doctor", "Check the environment", captured_action(["doctor"])),
        ("Logout", "Sign out of Google", captured_action(["logout"])),
    ]

    # ---- button grid ---------------------------------------------------------
    grid = tk.Frame(root, bg=BG)
    grid.pack(fill="x", padx=24)
    for col in (0, 1):
        grid.columnconfigure(col, weight=1, uniform="btn")

    def make_button(parent, label: str, hint: str, cmd: Callable[[], None]):
        btn = tk.Frame(parent, bg=BTN_BG, highlightthickness=1,
                       highlightbackground=BORDER, cursor="hand2")
        title = tk.Label(btn, text=label, bg=BTN_BG, fg=FG, font=(FONT, 11, "bold"))
        sub = tk.Label(btn, text=hint, bg=BTN_BG, fg=MUTED, font=(FONT, 8))
        title.pack(anchor="w", padx=14, pady=(10, 0))
        sub.pack(anchor="w", padx=14, pady=(0, 10))

        widgets = (btn, title, sub)

        def paint(colour: str) -> None:
            for w in widgets:
                w.configure(bg=colour)

        for w in widgets:
            w.bind("<Enter>", lambda _e: paint(BTN_HOVER))
            w.bind("<Leave>", lambda _e: paint(BTN_BG))
            w.bind("<Button-1>", lambda _e: cmd())
        return btn

    for i, (label, hint, cmd) in enumerate(actions):
        b = make_button(grid, label, hint, cmd)
        b.grid(row=i // 2, column=i % 2, sticky="nsew", padx=4, pady=4)

    out_frame.pack(fill="both", expand=True, padx=24, pady=(16, 24))
    show_output("ui", "Welcome. Interactive commands (Login, Run, Shell, Monitor) open "
                      "in their own terminal window; the others print here.")

    root.mainloop()
    return 0
