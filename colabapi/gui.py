"""The colabapi window (`colabapi ui`).

Everything happens here. Nothing opens a second terminal.

That is the whole design change from the first version of this window, which was
a launcher: each button shelled out to a *new terminal emulator* because the
interesting commands are interactive -- Google's sign-in prints a URL and waits,
`run` asks which runtime, the shell is a live pty. Sending the user out to an
xterm to answer those is not a UI; it is a menu that shows you the door.

So the window owns a real terminal (`termview.TerminalView`, a VT emulator) and
attaches things to it:

  * the runtime's shell -- `terminal.HardenedConsole` in embedded mode, the same
    reconnecting WebSocket client `colabapi shell` uses;
  * any colabapi command -- `localpty.LocalCommand`, run on a real pty, so Rich
    renders, prompts prompt, and answers can be typed straight into the window.

Both expose send/resize/close, so the window treats them identically and only
ever has one attached. Behaviour still lives in the CLI: the buttons run
`colabapi <command>`, they do not reimplement it.

Above the terminal sit live CPU / RAM / GPU / VRAM graphs (`gauges.py`) for the
selected session, and the session list is a real selection: choosing a row is
what Shell, Stop and Monitor act on, and it is what the graphs follow.

Tkinter is the toolkit because it ships with Python -- no runtime dependency for
a window most users open occasionally. Debian family distros split it into
`python3-tk`, so the import is wrapped and surfaced as TkUnavailable for the CLI
to explain (see cli.py).
"""

from __future__ import annotations

import os
import subprocess
import time
from queue import Empty, Queue
from typing import Optional

from . import gauges, localpty, timing
from .platform import IS_WINDOWS


class TkUnavailable(RuntimeError):
    """Raised when Tkinter cannot be imported or no display is reachable."""


def _import_tk():
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError as exc:
        raise TkUnavailable(str(exc)) from exc
    return tk, ttk, messagebox


# --------------------------------------------------------------------------- #
# palette / metrics -- one place, so the window stays coherent
# --------------------------------------------------------------------------- #
BG = "#ffffff"
FG = "#111111"
MUTED = "#6b6b6b"

FONT = "Segoe UI" if IS_WINDOWS else "DejaVu Sans"

# How often the session list is re-read from disk. It only changes when the user
# runs or stops something, so a slow poll is plenty -- and it costs one small
# JSON read.
SESSION_POLL_MS = 4000


def _signed_in() -> bool:
    """Google's CLI caches its token at this literal path on every platform."""
    return os.path.exists(os.path.expanduser(
        os.path.join("~", ".config", "colab-cli", "token.json")))


def _sessions() -> list:
    try:
        from .config import SessionStore
        return SessionStore.load().active()
    except Exception:  # noqa: BLE001 -- a corrupt store must not kill the window
        return []


def _logo_path() -> Optional[str]:
    try:
        from importlib.resources import files
        p = files("colabapi").joinpath("assets/colabapi.png")
        return str(p) if p.is_file() else None
    except Exception:  # noqa: BLE001 -- a missing asset only costs the icon
        return None


def _time_left(session) -> str:
    """What the list column shows: the number the user is actually watching.

    `timing.session_line` is the full sentence (uptime AND estimate AND the
    "(est.)" caveat) and it belongs under the graphs, where there is room. In a
    narrow column it just truncates, which is worse than useless -- "up 9h 42m ·
    ~2" tells you nothing. So the column carries the part that matters: how long
    this runtime has left.
    """
    if not session.started_at:
        return "—"
    rem = timing.remaining(session.started_at, session.max_lifetime_hours)
    if rem is None:
        return f"up {timing.human_duration(time.time() - session.started_at)}"
    if rem <= 0:
        return "past limit"
    return f"~{timing.human_duration(rem)} left"


def _open_stats_stream(session: str):
    """Start the live vitals feed inside a runtime. The graphs' transport.

    One connection for the life of the selection, not one per reading: connecting
    costs seconds, so a per-sample `colab exec` could never be live.
    """
    from . import monitor
    from .colabcli import ColabCLI

    colab = ColabCLI()
    colab.session = session
    return colab.exec_stream(monitor.STREAM_SNIPPET)


# --------------------------------------------------------------------------- #
# the window
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, tk, ttk, messagebox):
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox

        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            # No $DISPLAY (headless box, ssh without -X): config, not a bug.
            raise TkUnavailable(f"no display available ({exc})") from exc

        self.root.title("colabapi")
        self.root.configure(bg=BG)
        self.root.minsize(900, 640)
        self.root.geometry("1080x760")

        # What the terminal is wired to: a HardenedConsole, a LocalCommand, or
        # nothing. Exactly one, ever.
        self.attached = None
        self.attached_kind = ""
        self.selected: Optional[str] = None
        self._rows: list = []
        self._dead: set = set()          # session names whose runtime is gone
        self._closing = False
        self._samples: Queue = Queue()
        self._events: Queue = Queue()

        self._build()

        self.sampler = gauges.Sampler(_open_stats_stream, self._on_sample)
        self.sampler.start()

        # Reachability checker: a session can die on Colab's side (idle-out,
        # lifetime cap) while the local store lists it as active, and the list
        # must show that rather than let the user keep clicking Shell at a
        # corpse. Probing costs a network round trip per session, so it happens
        # on its own thread and on a slow clock, never on the UI thread.
        import threading
        threading.Thread(target=self._check_states_loop,
                         name="colabapi-statecheck", daemon=True).start()

        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self._poll_sessions()
        self._pump_samples()
        self._greet()

    # -- construction --------------------------------------------------------
    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        root = self.root

        self._icon()
        self._menus()

        # Header ---------------------------------------------------------------
        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=18, pady=(14, 10))
        if self._logo is not None:
            lab = tk.Label(header, image=self._logo, bg=BG)
            lab.image = self._logo
            lab.pack(side="left", padx=(0, 10))
        tk.Label(header, text="colabapi", bg=BG, fg=FG,
                 font=(FONT, 17, "bold")).pack(side="left")

        self.account = tk.StringVar()
        tk.Label(header, textvariable=self.account, bg=BG, fg=MUTED,
                 font=(FONT, 9)).pack(side="right")

        # The keep-alive switch. Same toggle as `colabapi daemon`: on spawns the
        # detached supervisor, off stops it. The variable is re-synced from the
        # real process state on every session poll, so a daemon toggled from a
        # terminal (or one that died) is reflected here within seconds.
        self.keepalive_var = tk.BooleanVar(value=False)
        self.keepalive_btn = ttk.Checkbutton(
            header, text="Keep alive", variable=self.keepalive_var,
            command=self._toggle_keepalive)
        self.keepalive_btn.pack(side="right", padx=(0, 16))
        self._sync_keepalive()

        # Graphs ---------------------------------------------------------------
        self.graphs = gauges.Graphs(root, bg=BG)
        self.graphs.pack(fill="x", padx=18)

        self.graph_note = tk.StringVar(value="No session selected.")
        tk.Label(root, textvariable=self.graph_note, bg=BG, fg=MUTED,
                 font=(FONT, 8)).pack(anchor="w", padx=18, pady=(4, 10))

        # Body: sessions on the left, terminal on the right --------------------
        body = tk.PanedWindow(root, orient="horizontal", bg=BG, sashwidth=6,
                              bd=0, relief="flat")
        body.pack(fill="both", expand=True, padx=18)

        left = tk.Frame(body, bg=BG)
        body.add(left, minsize=290, width=330)

        bar = tk.Frame(left, bg=BG)
        bar.pack(fill="x")
        tk.Label(bar, text="Sessions", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).pack(side="left")
        ttk.Button(bar, text="Refresh", width=8,
                   command=self.refresh).pack(side="right")

        self.tree = ttk.Treeview(left, columns=("runtime", "left"),
                                 show="tree headings", selectmode="browse", height=8)
        self.tree.heading("#0", text="Name")
        self.tree.heading("runtime", text="Runtime")
        self.tree.heading("left", text="Time left")
        self.tree.column("#0", width=130, anchor="w")
        self.tree.column("runtime", width=70, anchor="w")
        self.tree.column("left", width=110, anchor="w")
        self.tree.pack(fill="both", expand=True, pady=(6, 8))
        # A dead session is shown in red; the Delete action removes it.
        self.tree.tag_configure("dead", foreground="#cf222e")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        # Enter opens a shell on the highlighted row, so the list is fully usable
        # from the keyboard.
        self.tree.bind("<Return>", lambda _e: self.shell())
        self.tree.bind("<Double-Button-1>", lambda _e: self.shell())

        # Actions. Real ttk.Buttons, not click-bound labels: they take focus,
        # answer Space/Enter, and assistive tech can see them.
        actions = tk.Frame(left, bg=BG)
        actions.pack(fill="x", pady=(0, 10))
        for c in (0, 1):
            actions.columnconfigure(c, weight=1, uniform="a")

        self.buttons: dict = {}
        specs = [
            ("New runtime", self.new_runtime),
            ("Shell", self.shell),
            ("Stop", self.stop),
            ("Monitor", self.monitor),
            ("Delete", self.delete),
            ("Sign in", self.login),
            ("Sign out", self.logout),
        ]
        for i, (label, cmd) in enumerate(specs):
            b = ttk.Button(actions, text=label, command=cmd)
            b.grid(row=i // 2, column=i % 2, sticky="ew",
                   padx=(0 if i % 2 == 0 else 4, 0), pady=2)
            self.buttons[label] = b

        right = tk.Frame(body, bg=BG)
        body.add(right, minsize=420)

        tbar = tk.Frame(right, bg=BG)
        tbar.pack(fill="x")
        self.term_title = tk.StringVar(value="Terminal")
        tk.Label(tbar, textvariable=self.term_title, bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).pack(side="left")
        self.detach_btn = ttk.Button(tbar, text="Disconnect", width=13,
                                     command=self.detach, state="disabled")
        self.detach_btn.pack(side="right")
        ttk.Button(tbar, text="Clear", width=7,
                   command=lambda: self.term.reset()).pack(side="right", padx=(0, 6))

        from . import termview
        self.term = termview.TerminalView(right)
        self.term.pack(fill="both", expand=True, pady=(6, 10))

        # Status bar ------------------------------------------------------------
        self.status = tk.StringVar(value="")
        tk.Label(root, textvariable=self.status, bg=BG, fg=MUTED, anchor="w",
                 font=(FONT, 9)).pack(fill="x", padx=18, pady=(0, 10))

        for seq, fn in (("<Control-n>", self.new_runtime), ("<Control-s>", self.shell),
                        ("<Control-k>", self.stop), ("<Control-m>", self.monitor),
                        ("<Control-l>", self.login), ("<F5>", self.refresh)):
            root.bind_all(seq, lambda _e, f=fn: f())

    def _icon(self) -> None:
        tk = self.tk
        self._logo = None
        path = _logo_path()
        if not path:
            return
        try:
            img = tk.PhotoImage(file=path)
            self.root.iconphoto(True, img)
            self._logo_full = img          # keep a reference or Tk collects it
            self._logo = img.subsample(max(img.width() // 30, 1))
        except tk.TclError:
            self._logo = None

    def _menus(self) -> None:
        tk = self.tk
        bar = tk.Menu(self.root)

        session = tk.Menu(bar, tearoff=0)
        session.add_command(label="New runtime…", accelerator="Ctrl+N",
                            command=self.new_runtime)
        session.add_command(label="Open shell", accelerator="Ctrl+S", command=self.shell)
        session.add_command(label="Monitor", accelerator="Ctrl+M", command=self.monitor)
        session.add_command(label="Python REPL", command=self.repl)
        session.add_separator()
        session.add_command(label="Stop session…", accelerator="Ctrl+K", command=self.stop)
        session.add_command(label="Refresh", accelerator="F5", command=self.refresh)
        session.add_separator()
        session.add_command(label="Quit", command=self.quit)
        bar.add_cascade(label="Session", menu=session)

        account = tk.Menu(bar, tearoff=0)
        account.add_command(label="Sign in to Google", accelerator="Ctrl+L",
                            command=self.login)
        account.add_command(label="Sign out", command=self.logout)
        bar.add_cascade(label="Account", menu=account)

        tools = tk.Menu(bar, tearoff=0)
        tools.add_command(label="Status", command=self.status_cmd)
        tools.add_command(label="List sessions", command=lambda: self.run_cli(["sessions"], "sessions"))
        tools.add_command(label="List runtimes", command=lambda: self.run_cli(["runtimes"], "runtimes"))
        tools.add_command(label="Doctor", command=lambda: self.run_cli(["doctor"], "doctor"))
        tools.add_separator()
        svc = tk.Menu(tools, tearoff=0)
        svc.add_command(label="Install keep-alive service",
                        command=lambda: self.run_cli(["service", "install"], "service install"))
        svc.add_command(label="Service status",
                        command=lambda: self.run_cli(["service", "status"], "service status"))
        svc.add_command(label="Uninstall service",
                        command=lambda: self.run_cli(["service", "uninstall"], "service uninstall"))
        tools.add_cascade(label="Keep-alive service", menu=svc)
        bar.add_cascade(label="Tools", menu=tools)

        self.root.config(menu=bar)

    # -- keep-alive toggle -----------------------------------------------------
    def _toggle_keepalive(self) -> None:
        from . import daemonctl

        try:
            if self.keepalive_var.get():
                pid = daemonctl.start()
                self.say(f"Keep-alive on: supervising every session in the background (pid {pid}).")
            else:
                daemonctl.stop()
                self.say("Keep-alive off. Runtimes can idle out without it.")
        except Exception as exc:  # noqa: BLE001 -- a failed toggle must not kill the window
            self.say(f"Keep-alive toggle failed: {exc}")
        self._sync_keepalive()

    def _sync_keepalive(self) -> None:
        """Make the switch show the truth: the process, not the last click."""
        from . import daemonctl

        try:
            self.keepalive_var.set(daemonctl.is_running())
        except Exception:  # noqa: BLE001
            pass

    # -- session list --------------------------------------------------------
    def _poll_sessions(self) -> None:
        if self._closing:
            return
        self.refresh()
        self._sync_keepalive()
        self.root.after(SESSION_POLL_MS, self._poll_sessions)

    def refresh(self) -> None:
        rows = _sessions()
        self._rows = rows
        names = [s.name for s in rows]

        existing = set(self.tree.get_children())
        for gone in existing - set(names):
            self.tree.delete(gone)
        self._dead &= set(names)
        for s in rows:
            dead = s.name in self._dead
            values = (s.runtime, "expired" if dead else _time_left(s))
            tags = ("dead",) if dead else ()
            if s.name in existing:
                self.tree.item(s.name, values=values, tags=tags)
            else:
                self.tree.insert("", "end", iid=s.name, text=s.name,
                                 values=values, tags=tags)

        # Keep a selection alive: the graphs and every session action key off it,
        # and an empty selection after a refresh would silently disarm them.
        if self.selected not in names:
            self.selected = None
        if self.selected is None and names:
            self.selected = names[0]
            self.graph_note.set(f"Reading {self.selected}…")
        if self.selected:
            self.tree.selection_set(self.selected)

        self.sampler.watch(self.selected)
        if self.selected is None:
            self.graphs.update_from(None)
            self.graph_note.set(
                "No runtime yet. Use New runtime (Ctrl+N) to allocate one."
                if _signed_in() else
                "Not signed in. Use Sign in (Ctrl+L) — Google's own browser flow.")

        n = len(names)
        self.account.set(
            f"{'Signed in to Google' if _signed_in() else 'Not signed in'}   ·   "
            f"{n} active session{'' if n == 1 else 's'}")
        self._arm()

    def _arm(self) -> None:
        """Enable only what can actually be done right now."""
        state = "normal" if self.selected is not None else "disabled"
        for label in ("Shell", "Stop", "Monitor"):
            self.buttons[label].configure(state=state)
        # Delete is the dead-session action: it removes an expired entry
        # without the "anything running on it ends" ceremony, because nothing
        # is running on it.
        self.buttons["Delete"].configure(
            state="normal" if self.selected in self._dead else "disabled")

    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        name = sel[0] if sel else None
        if name == self.selected:
            return
        self.selected = name
        self.graphs.update_from(None)
        self.graph_note.set(f"Reading {name}…" if name else "No session selected.")
        self.sampler.watch(name)
        self._arm()

    def _need(self) -> Optional[str]:
        """The selected session, or a nudge to pick one.

        Shell / Stop / Monitor act on *a* session, so the window refuses to
        guess: with nothing selected it says so and puts the user in the list.
        """
        if self.selected:
            return self.selected
        if not self._rows:
            self.say("No sessions yet — use New runtime (Ctrl+N) to allocate one.")
        else:
            self.say("Select a session in the list first.")
            self.tree.focus_set()
        return None

    # -- graphs --------------------------------------------------------------
    def _on_sample(self, stats: Optional[dict], reason: str) -> None:
        """Called on the sampler thread -- so it must not touch Tk at all.

        Not even `after()`: registering a Tcl command off the main thread is what
        raises "main thread is not in main loop". The reading goes in a queue and
        the UI thread collects it on its own clock (`_pump_samples`).
        """
        self._samples.put((stats, reason))

    def _pump_samples(self) -> None:
        """The UI thread's inbox: everything the worker threads posted."""
        if self._closing:
            return
        latest = None
        while True:
            try:
                latest = self._samples.get_nowait()
            except Empty:
                break
        if latest is not None:
            # Only the newest reading matters: an older one is a stale picture of
            # a machine that has since moved on.
            self._paint_sample(*latest)

        while True:
            try:
                event = self._events.get_nowait()
            except Empty:
                break
            if event == "command_finished":
                self._command_finished()
            elif isinstance(event, tuple) and event[0] == "states":
                self._apply_states(event[1])
        self.root.after(250, self._pump_samples)

    def _check_states_loop(self) -> None:
        """Worker thread: probe every session's runtime, post the verdicts."""
        import time as _time

        while not self._closing:
            try:
                from .cli import _session_states

                rows = _sessions()
                states = _session_states(rows) if rows else {}
                self._events.put(("states", states))
            except Exception:  # noqa: BLE001 -- a failed probe round is just skipped
                pass
            _time.sleep(20)

    def _apply_states(self, states: dict) -> None:
        """UI thread: paint the verdicts the checker posted."""
        dead = {name for name, reason in states.items() if reason is not None}
        if dead == self._dead:
            return
        self._dead = dead
        self.refresh()
        if self.selected in dead:
            self.graph_note.set(f"{self.selected}: expired - the runtime is gone. "
                                "Use Delete to remove it.")

    def _paint_sample(self, stats: Optional[dict], reason: str) -> None:
        if self._closing:
            return
        self.graphs.update_from(stats, reason)
        if self.selected is None:
            return
        if stats:
            s = next((x for x in self._rows if x.name == self.selected), None)
            line = (timing.session_line(s.started_at, s.max_lifetime_hours)
                    if s and s.started_at else "")
            self.graph_note.set(f"{self.selected} · live from inside the runtime"
                                + (f" · {line}" if line else ""))
        elif reason:
            self.graph_note.set(f"{self.selected}: {reason}")

    # -- the terminal --------------------------------------------------------
    def _attach(self, obj, title: str, kind: str) -> None:
        self.detach()
        self.attached = obj
        self.attached_kind = kind
        self.term.set_handlers(obj.send, obj.resize)
        self.term.reset()
        self.term_title.set(title)
        self.detach_btn.configure(
            state="normal",
            text="Disconnect" if kind == "shell" else "Stop command")
        self.term.text.focus_set()
        cols, rows = self.term.size()
        obj.resize(cols, rows)

    def detach(self) -> None:
        """Let go of whatever the terminal holds. The runtime keeps running --
        this hangs up, it does not stop anything."""
        if self.attached is not None:
            try:
                self.attached.close()
            except Exception:  # noqa: BLE001
                pass
        self.attached = None
        self.attached_kind = ""
        self.term.set_handlers(None)
        self.term_title.set("Terminal")
        self.detach_btn.configure(state="disabled", text="Disconnect")

    def run_cli(self, args: list, title: str) -> None:
        """Run `colabapi <args>` on a pty, inside the window.

        The pty is the point: these commands prompt, and they render with Rich.
        Captured on a pipe they would print nothing until they exited, and there
        would be nowhere to type the answer.
        """
        args = [a for a in args if a]
        if not localpty.available():
            self._windows_fallback(args)
            return

        cols, rows = self.term.size()

        def done(code: int) -> None:
            # Runs on the pty thread. `term.feed` is queue-backed and safe; the
            # UI work is not, so it is posted rather than performed here (same
            # rule as _on_sample).
            self.term.feed(
                f"\r\n\x1b[36m[colabapi]\x1b[0m {title} finished"
                + (f" (exit {code})" if code not in (0, -1) else "") + "\r\n")
            self._events.put("command_finished")

        proc = localpty.LocalCommand(localpty.cli_argv() + args, self.term.feed,
                                     on_exit=done, cols=cols, rows=rows)
        try:
            proc.start()
        except OSError as exc:
            self.say(f"Could not run colabapi {' '.join(args)}: {exc}")
            return
        self._attach(proc, f"colabapi {' '.join(args)}", "command")

    def _command_finished(self) -> None:
        if self.attached_kind != "command":
            return
        self.attached = None
        self.attached_kind = ""
        self.term.set_handlers(None)
        self.detach_btn.configure(state="disabled")
        # Sessions and sign-in state are precisely what these commands change.
        self.refresh()

    def _windows_fallback(self, args: list) -> None:
        """No pty on Windows: keep the old new-console path, and say so rather
        than silently doing nothing."""
        try:
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(["cmd.exe", "/k"] + localpty.cli_argv() + args,
                             creationflags=flags)
            self.say("Windows has no pty, so this opened in a console window.")
        except OSError as exc:
            self.say(f"Could not run: {exc}")

    # -- actions -------------------------------------------------------------
    def login(self) -> None:
        self.run_cli(["login"], "sign-in")
        self.say("Signing in — Google opens a browser. Answer any prompt below.")

    def logout(self) -> None:
        if not self.messagebox.askyesno(
                "Sign out",
                "Sign out of Google and forget every session?\n\n"
                "Runtimes already allocated keep running on Colab's side until "
                "Colab's own timers end them.",
                parent=self.root):
            return
        self.detach()
        self.run_cli(["logout"], "sign-out")

    def new_runtime(self) -> None:
        self.run_cli(["run"], "run")
        self.say("Choose a runtime and name it in the terminal below.")

    def shell(self) -> None:
        name = self._need()
        if not name:
            return
        from . import terminal

        console = terminal.HardenedConsole(
            name, sink=self.term.feed, note=self.term.write_note,
            size_fn=self.term.size, persist=True)
        self._attach(console, f"Shell · {name}", "shell")
        console.start()
        self.say(f"Connected to {name}. Your work survives a drop (it runs in tmux).")

    def repl(self) -> None:
        name = self._need()
        if name:
            self.run_cli(["repl", name], f"repl {name}")

    def status_cmd(self) -> None:
        name = self._need()
        if name:
            self.run_cli(["status", name], f"status {name}")

    def monitor(self) -> None:
        name = self._need()
        if not name:
            return
        self.run_cli(["monitor", name], f"monitor {name}")
        self.say("The graphs above show the same numbers, live.")

    def stop(self) -> None:
        name = self._need()
        if not name:
            return
        if not self.messagebox.askyesno(
                "Stop session",
                f"Stop '{name}' and release the runtime?\n\n"
                "The GPU goes back to Colab and anything running on it ends.",
                parent=self.root):
            return
        if self.attached_kind == "shell":
            self.detach()          # never leave a shell attached to a dead VM
        self.run_cli(["stop", name], f"stop {name}")

    def delete(self) -> None:
        """Remove an expired session's stale entry. `stop` under the hood."""
        name = self._need()
        if not name:
            return
        if name not in self._dead:
            self.say("Delete is for expired sessions; use Stop for a live one.")
            return
        if self.attached_kind == "shell":
            self.detach()
        self.run_cli(["stop", name], f"delete {name}")
        self.say(f"Removing the expired session '{name}'.")

    # -- misc ----------------------------------------------------------------
    def say(self, message: str) -> None:
        self.status.set(message)

    def _greet(self) -> None:
        self.term.feed(
            "\x1b[36mcolabapi\x1b[0m — everything runs in this window.\r\n\r\n"
            "  \x1b[1mNew runtime\x1b[0m  allocate a Colab GPU        (Ctrl+N)\r\n"
            "  \x1b[1mShell\x1b[0m        a terminal on the selected session (Ctrl+S)\r\n"
            "  \x1b[1mStop\x1b[0m         release the runtime         (Ctrl+K)\r\n\r\n"
            "Select a session on the left; the graphs above follow it.\r\n")

    def quit(self) -> None:
        self._closing = True
        self.sampler.stop()
        self.detach()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run() -> int:
    """Build and run the window. Returns a process exit code."""
    tk, ttk, messagebox = _import_tk()
    try:
        import pyte  # noqa: F401
    except ImportError as exc:
        # The embedded terminal is the window's reason to exist, so a missing
        # emulator is a hard failure with a fix, not a silent degradation.
        raise TkUnavailable(
            "the embedded terminal needs `pyte` — install it with: pip install pyte"
        ) from exc
    return App(tk, ttk, messagebox).run()
