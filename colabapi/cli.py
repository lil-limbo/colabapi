"""colabapi command-line interface.

colabapi wraps Google's official `colab` CLI, adding a single command, a systemd
service, a runtime picker, a live resource monitor, and session-time display.
Authentication, runtime allocation, and the interactive terminal are delegated to
Google's own tool (the ban-safe path); this module is the friendly orchestration
layer on top.
"""

from __future__ import annotations

import os
import shutil
import sys
import time

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import (
    __version__, monitor as monitor_mod, persist, platform as plat,
    runtime as rt, service, shellview, terminal, timing, ui,
)
from .colabcli import ColabCLI, ColabCliNotFound, INSTALL_HINT
from .config import Config, Session, SessionStore, ensure_dirs
from .keepalive import KeepAliveSupervisor

console = Console()
colab = ColabCLI()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _require_colab() -> None:
    if not colab.available():
        console.print(Panel.fit(INSTALL_HINT, title="Missing dependency", border_style="red"))
        raise SystemExit(1)


def _check_on_path() -> None:
    """Warn when `colabapi` is installed somewhere that is not on PATH.

    A plain `pipx install` / `pip install --user` as root drops the script into
    /root/.local/bin, which most distros do not put on root's PATH. The install
    then succeeds but `colabapi` reports "command not found", so we say exactly
    where it landed and how to fix it. Only reachable when the user invoked us by
    absolute path (otherwise, by definition, we are already on PATH).
    """
    here = os.path.dirname(os.path.abspath(sys.argv[0] or ""))
    if not here or shutil.which("colabapi"):
        console.print("colabapi on PATH: [green]yes[/]")
        return
    console.print(f"colabapi on PATH: [yellow]no[/] (installed at {here})")
    fix = f'export PATH="{here}:$PATH"'
    console.print(Panel.fit(
        f"`colabapi` is not on your PATH, so the bare command will not work.\n\n"
        f"Add it for this shell:\n  {fix}\n\n"
        f"Make it permanent:\n  echo '{fix}' >> ~/.bashrc\n\n"
        f"Or reinstall system-wide (recommended as root):\n"
        f"  pipx install --global --force colabapi",
        title="Not on PATH", border_style="yellow"))


def _session_label(s: Session) -> str:
    line = timing.session_line(s.started_at, s.max_lifetime_hours) if s.started_at else ""
    return f"{s.name}  [{s.runtime}]  {line}".rstrip()


def _pick_session(action: str, name: str | None = None) -> Session:
    """Resolve which session to act on: by explicit name, the only one, or a picker."""
    store = SessionStore.load()
    active = store.active()
    if not active:
        console.print("[red]No active runtime.[/] Start one with [bold]colabapi run[/].")
        raise SystemExit(1)
    if name:
        s = store.get(name)
        if not s:
            console.print(f"[red]No session named '{name}'.[/] "
                          f"Known: {', '.join(x.name for x in active) or '(none)'}")
            raise SystemExit(1)
    elif len(active) == 1:
        s = active[0]
    else:
        s = ui.select(active, title=f"Select a session to {action}:", to_label=_session_label)
        if s is None:
            console.print("[dim]Cancelled.[/]")
            raise SystemExit(0)
    colab.session = s.name  # target this exact session for all follow-up calls
    return s


def _default_session_name() -> str:
    import uuid
    return "colab-" + uuid.uuid4().hex[:4]


def _sanitize_name(raw: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_") else "-" for c in raw.strip())
    return keep.strip("-_") or _default_session_name()


def _run_remote(code: str) -> str:
    res = colab.exec_code(code)
    return res.stdout if res.stdout.strip() else res.stderr


def _max_lifetime_for(runtime_key: str) -> float:
    r = rt.get(runtime_key)
    if r and r.tier in ("pro", "pro+", "paid"):
        return 24.0
    return 12.0


# --------------------------------------------------------------------------- #
# group
# --------------------------------------------------------------------------- #
@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="colabapi")
def cli() -> None:
    """Run and reach a persistent Google Colab runtime from your own terminal.

    colabapi never handles your Google password. It drives Google's official
    `colab` CLI: you sign in through Google's own browser flow, and the runtime
    terminal comes over Google's sanctioned tunnel.
    """
    ensure_dirs()


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #
@cli.command()
def login() -> None:
    """Sign in to Colab through Google's browser flow (no password asked)."""
    _require_colab()
    console.print(Panel.fit(
        "colabapi hands off to Google's official sign-in.\n\n"
        "If this is your first sign-in, a browser window opens for [bold]Google's\n"
        "own login[/] (including any 2FA or device checks). [bold]Your password\n"
        "never touches colabapi.[/] If you're already signed in, this just lists\n"
        "your sessions.",
        title="Sign in to Colab", border_style="cyan"))
    code = colab.login()
    if code == 0:
        console.print("[green]Signed in.[/] Next: [bold]colabapi run[/]")
    else:
        console.print(f"[yellow]Sign-in exited with code {code}.[/] Try again or run [bold]colabapi doctor[/].")


# --------------------------------------------------------------------------- #
# logout
# --------------------------------------------------------------------------- #
@cli.command()
def logout() -> None:
    """Sign out of Google and forget every session, so you can start over.

    Google's CLI caches its OAuth token at ~/.config/colab-cli/token.json (the
    same literal path on every platform, Windows included, because Google
    hardcodes it via expanduser). Removing that file is what actually signs the
    user out; we also drop Google's session list and colabapi's own bookkeeping
    so `sessions` and `shell` start from a clean slate. The OAuth *client*
    config (~/.colab-cli-oauth-config.json) is deliberately left alone: it holds
    no credentials, and deleting it would break the next login.
    """
    removed = False

    # Google's credential cache and session list. expanduser, not config_dir():
    # these paths belong to Google's CLI, and it puts them under ~/.config on
    # every platform.
    for fname in ("token.json", "sessions.json"):
        path = os.path.expanduser(os.path.join("~", ".config", "colab-cli", fname))
        try:
            if os.path.exists(path):
                os.remove(path)
                removed = True
        except OSError as exc:
            console.print(f"[yellow]Could not remove {path}:[/] {exc}")

    # colabapi's own session registry (the store behind `sessions` / `shell`).
    try:
        store = SessionStore.load()
        if store.sessions:
            store.sessions = []
            store.save()
            removed = True
    except Exception:  # noqa: BLE001 -- a corrupt store must not block signing out
        try:
            from .config import SESSIONS_FILE
            SESSIONS_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    if removed:
        console.print("[green]Signed out of Google.[/] Run [bold]colabapi login[/] to sign in again.")
        console.print("[dim]Any runtimes still allocated on Colab's side keep running until "
                      "Colab's timers end them.[/]")
    else:
        console.print("You are already signed out.")


# --------------------------------------------------------------------------- #
# ui
# --------------------------------------------------------------------------- #
# Named explicitly: the function cannot be called `ui` without shadowing the
# `ui` picker module imported at the top of this file.
@cli.command(name="ui")
def ui_cmd() -> None:
    """Open the colabapi desktop window (a graphical front end for the CLI)."""
    try:
        from . import gui
    except ImportError:
        _tk_missing_help()
        raise SystemExit(1)
    try:
        code = gui.run()
    except gui.TkUnavailable as exc:
        _tk_missing_help(str(exc))
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 -- a broken display must not traceback
        console.print(f"[red]Could not open the colabapi window:[/] {exc}")
        console.print("[dim]All the same actions work from the command line "
                      "(see `colabapi --help`).[/]")
        raise SystemExit(1)
    raise SystemExit(code)


def _tk_missing_help(detail: str = "") -> None:
    """Explain how to get Tkinter, the one piece pip cannot install."""
    if "display" in detail:
        # Tkinter is fine; there is just no screen (headless box, plain ssh).
        console.print(Panel.fit(
            "No graphical display is reachable, so the window cannot open.\n"
            f"[dim]{detail}[/]\n\n"
            "Run this on a machine with a desktop session (or connect with\n"
            "X forwarding: [bold]ssh -X[/]). Every action in the window is\n"
            "also a plain command; see [bold]colabapi --help[/].",
            title="colabapi ui", border_style="yellow"))
        return
    lines = ["The graphical window needs Tkinter, which is part of Python itself\n"
             "but is sometimes packaged separately."]
    if detail:
        lines.append(f"\n[dim]{detail}[/]")
    if plat.IS_LINUX:
        lines.append("\nInstall it, then retry:\n  [bold]sudo apt install python3-tk[/]   (Debian / Ubuntu / Kali)\n"
                     "  [bold]sudo dnf install python3-tkinter[/]   (Fedora)")
    else:
        lines.append("\nReinstall Python from python.org with the default options\n"
                     "(they include Tkinter), then retry.")
    console.print(Panel.fit("".join(lines), title="colabapi ui", border_style="yellow"))


# --------------------------------------------------------------------------- #
# runtimes
# --------------------------------------------------------------------------- #
@cli.command()
def runtimes() -> None:
    """List Colab runtime types and which ones need a paid plan."""
    _print_runtimes()


def _print_runtimes() -> None:
    table = Table(title="Colab runtimes", header_style="bold cyan")
    table.add_column("Key")
    table.add_column("Runtime")
    table.add_column("Accelerator")
    table.add_column("Availability")
    table.add_column("Notes", style="dim")
    for r in rt.RUNTIMES:
        avail = "[green]available[/]" if r.free else \
            f"[yellow]needs {r.tier.upper()}[/] (not on a free account)"
        table.add_row(r.key, r.label, r.accelerator, avail, r.notes)
    console.print(table)
    console.print("[dim]Free-account requests for paid runtimes are refused by Colab itself.[/]")


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
@cli.command()
@click.option("--runtime", "runtime_key", default=None, help="Runtime key (see `colabapi runtimes`).")
@click.option("--yes", is_flag=True, help="Skip confirmation prompts.")
def run(runtime_key: str | None, yes: bool) -> None:
    """Allocate a Colab runtime (delegates to `colab new`)."""
    _require_colab()
    cfg = Config.load()

    _print_runtimes()
    if not runtime_key:
        runtime_key = Prompt.ask("Runtime", default=cfg.default_runtime,
                                 choices=[r.key for r in rt.RUNTIMES])
    chosen = rt.get(runtime_key)
    if not chosen:
        console.print(f"[red]Unknown runtime '{runtime_key}'.[/]")
        raise SystemExit(1)

    if not chosen.free and not yes:
        console.print(Panel.fit(
            f"[yellow]{chosen.label}[/] needs [bold]{chosen.tier.upper()}[/]. If your account\n"
            "isn't subscribed, Colab will refuse to allocate it.",
            border_style="yellow"))
        if not Confirm.ask("Continue anyway?", default=False):
            raise SystemExit(0)

    # Ask the user to name this session; used as `colab new -s <name>` and for
    # every later command (shell / stop / monitor).
    store = SessionStore.load()
    suggested = _default_session_name()
    while True:
        raw = Prompt.ask("Name this session", default=suggested)
        name = _sanitize_name(raw)
        if store.get(name):
            console.print(f"[yellow]You already have a session named '{name}'.[/] Pick another.")
            suggested = _default_session_name()
            continue
        break

    flags = " ".join(chosen.colab_flags)
    console.print(f"Requesting [cyan]{chosen.label}[/] via [bold]colab new -s {name} {flags}[/]…")
    console.print("[dim]If you're not signed in yet, a browser opens for Google's login first.[/]\n")
    code = colab.new_runtime(chosen.colab_flags, name=name)
    if code != 0:
        console.print(f"\n[red]colab new failed (exit {code}).[/] "
                      "Run [bold]colabapi login[/] then retry, or [bold]colabapi doctor[/].")
        raise SystemExit(1)

    s = Session(runtime=chosen.key, started_at=time.time(),
                max_lifetime_hours=_max_lifetime_for(chosen.key), name=name)
    store.add(s)
    cfg.default_runtime = chosen.key
    cfg.save()

    console.print(f"[green]Runtime '{name}' ready.[/] {timing.session_line(s.started_at, s.max_lifetime_hours)}")
    console.print(f"\nNow: [bold]colabapi shell[/] · [bold]colabapi monitor[/] · [bold]colabapi stop {name}[/]")
    console.print("[dim]Tip: `colabapi service install` restarts the keep-alive after a logout "
                  "or reboot, so the session survives your machine sleeping.[/]")
    console.print(f"[dim]{persist.detach_hint(name)}[/]")


# --------------------------------------------------------------------------- #
# shell / repl
# --------------------------------------------------------------------------- #
@cli.command()
@click.argument("name", required=False)
def shell(name: str | None) -> None:
    """Open a terminal on a session, with a live monitor on top.

    With no NAME and several sessions active, an arrow-key picker appears.
    """
    _require_colab()
    s = _pick_session("open a shell on", name)
    shellview.open_shell(colab, s.name)


@cli.command()
@click.argument("name", required=False)
def repl(name: str | None) -> None:
    """Open an interactive Python REPL on a session (`colab repl`)."""
    _require_colab()
    _pick_session("open a REPL on", name)
    colab.repl()


# --------------------------------------------------------------------------- #
# monitor
# --------------------------------------------------------------------------- #
@cli.command()
@click.argument("name", required=False)
def monitor(name: str | None) -> None:
    """Live CPU / RAM / GPU monitor for a session (Ctrl-C to exit)."""
    _require_colab()
    s = _pick_session("monitor", name)

    def line() -> str:
        return f"{s.name} · {timing.session_line(s.started_at, s.max_lifetime_hours)}"

    monitor_mod.live_monitor(_run_remote, line, interval=Config.load().monitor_interval)
    console.print("[dim]Monitor closed.[/]")


# Hidden: the resilient console that runs inside the bottom tmux pane of `shell`.
@cli.command(name="_paneconsole", hidden=True)
@click.argument("name")
def _paneconsole(name: str) -> None:
    """Internal: the reconnecting terminal for one named session (used by `shell`)."""
    raise SystemExit(terminal.open_console(name))


# Hidden: the monitor process that runs inside the top tmux pane of `shell`.
@cli.command(name="_panemonitor", hidden=True)
@click.argument("name")
def _panemonitor(name: str) -> None:
    """Internal: live monitor for a single named session (used by `shell`)."""
    colab.session = name
    s = SessionStore.load().get(name)

    def line() -> str:
        if s and s.started_at:
            return f"{name} · {timing.session_line(s.started_at, s.max_lifetime_hours)}"
        return name

    monitor_mod.live_monitor(_run_remote, line, interval=max(Config.load().monitor_interval, 2.0))


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
@cli.command()
def sessions() -> None:
    """List the sessions colabapi manages."""
    active = SessionStore.load().active()
    if not active:
        console.print("No active sessions. Start one with [bold]colabapi run[/].")
        return
    table = Table(title="colabapi sessions", header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Runtime")
    table.add_column("Uptime / est. remaining")
    for s in active:
        table.add_row(s.name, s.runtime, timing.session_line(s.started_at, s.max_lifetime_hours))
    console.print(table)


@cli.command()
@click.argument("name", required=False)
def status(name: str | None) -> None:
    """Show a session's reachability and estimated time remaining.

    With no NAME and several sessions active, an arrow-key picker appears.
    """
    s = _pick_session("check", name)
    table = Table(show_header=False, box=None)
    table.add_row("Session", f"[cyan]{s.name}[/]")
    table.add_row("Runtime", s.runtime)
    table.add_row("Uptime / est.", timing.session_line(s.started_at, s.max_lifetime_hours))
    if colab.available():
        res = colab.status()  # colab.session already set by _pick_session
        reach = "[green]reachable[/]" if res.ok else "[red]unreachable (session may have ended)[/]"
        table.add_row("colab status", reach)
        if res.text.strip():
            table.add_row("", f"[dim]{res.text.strip().splitlines()[0]}[/]")
    if service.is_installed():
        table.add_row("Service", "installed")
    console.print(Panel(table, title="colabapi status", border_style="cyan"))


# --------------------------------------------------------------------------- #
# stop
# --------------------------------------------------------------------------- #
@cli.command()
@click.argument("name", required=False)
@click.option("--keep-remote", is_flag=True,
              help="Only forget the session locally; leave the Colab runtime running.")
def stop(name: str | None, keep_remote: bool) -> None:
    """Stop a session (`colab stop`) and remove it from colabapi.

    Give a NAME (e.g. `colabapi stop my-run`), or run `colabapi stop` with no name
    to pick from an arrow-key list of active sessions.
    """
    s = _pick_session("stop", name)
    store = SessionStore.load()
    if not keep_remote and colab.available():
        console.print(f"Stopping [cyan]{s.name}[/] via [bold]colab stop[/]…")
        res = colab.stop_session()  # colab.session already set by _pick_session
        if res.ok:
            console.print("[green]Runtime stopped and released.[/]")
        else:
            first = res.text.strip().splitlines()[0] if res.text.strip() else ""
            console.print(f"[yellow]colab stop exited {res.returncode}.[/] {first}")
            console.print("[dim]You can also stop it in the Colab UI (Runtime → Disconnect and delete runtime).[/]")
    store.remove(s.name)
    console.print(f"[green]Removed '{s.name}' from colabapi.[/]")


# --------------------------------------------------------------------------- #
# daemon (systemd)
# --------------------------------------------------------------------------- #
@cli.command()
@click.argument("name", required=False)
@click.option("--foreground", is_flag=True, help="Run in the foreground (used by the service).")
def daemon(name: str | None, foreground: bool) -> None:
    """Keep the runtime alive, and restart the keep-alive if it ever dies.

    The actual ping is Google's own: their CLI ships a keep-alive daemon that
    calls Colab's tunnel endpoint every 60s. The problem is that they spawn it as
    a child of your terminal, so it dies when your machine sleeps, you log out,
    or it reboots -- and the runtime then idles out for no good reason.

    This supervises that daemon and restarts it whenever it is missing (it also
    deliberately exits every 24h on its own). Run it under `colabapi service
    install` and the session survives logout and reboot.

    With --foreground and no NAME (how the installed service runs it), every
    active session is supervised, sessions created later are picked up, and an
    empty session list means "wait", not "exit" -- a service that exits because
    there is nothing to do yet would just be restart-looped by systemd.
    """
    _require_colab()
    if foreground and not name:
        _daemon_all()
        return
    s = _pick_session("supervise", name)
    console.print(f"[cyan]colabapi keep-alive supervisor started for '{s.name}'[/]")

    sup = KeepAliveSupervisor(s.name, on_event=lambda m: console.print(f"[dim]{m}[/]"))
    sup.start()
    try:
        while True:
            if sup.gave_up:
                console.print(f"[red]{sup.health.reason}[/]")
                break
            rem = timing.remaining(s.started_at, s.max_lifetime_hours)
            if rem is not None and rem <= 0:
                # Colab's absolute lifetime cap is enforced server-side. Pinging
                # past it achieves nothing, so we stop rather than hammer it.
                console.print("[yellow]Past Colab's max lifetime cap; the runtime is gone. "
                              "Start a new one with `colabapi run`.[/]")
                break
            time.sleep(15)
            if not foreground:
                break
    except KeyboardInterrupt:
        pass
    finally:
        sup.stop()
        console.print("[dim]Supervisor stopped. (The runtime itself keeps running.)[/]")


def _daemon_all() -> None:
    """Service mode: supervise every active session, forever.

    Re-reads the session store each cycle so sessions started after the service
    (the normal order of events after a reboot) get picked up without a restart.
    A session is dropped from supervision -- but never re-added in the same
    process -- once its supervisor gives up or it passes Colab's lifetime cap;
    keying the retired set on (name, started_at) means a *new* session reusing
    the name is supervised again.
    """
    console.print("[cyan]colabapi keep-alive supervisor (service mode: all sessions)[/]")
    sups: dict[str, KeepAliveSupervisor] = {}
    retired: set[tuple[str, float | None]] = set()
    said_waiting = False
    try:
        while True:
            active = {s.name: s for s in SessionStore.load().active()}
            # Start supervising anything new.
            for sname, s in active.items():
                if sname in sups or (sname, s.started_at) in retired:
                    continue
                rem = timing.remaining(s.started_at, s.max_lifetime_hours)
                if rem is not None and rem <= 0:
                    # Past the server-side cap: pinging achieves nothing.
                    retired.add((sname, s.started_at))
                    console.print(f"[yellow]'{sname}' is past Colab's max lifetime cap; not supervising.[/]")
                    continue
                sup = KeepAliveSupervisor(
                    sname, on_event=lambda m, n=sname: console.print(f"[dim][{n}] {m}[/]"))
                sup.start()
                sups[sname] = sup
                console.print(f"[green]supervising '{sname}'[/]")
            # Retire what is finished.
            for sname in list(sups):
                s = active.get(sname)
                gone = s is None
                capped = (s is not None
                          and (rem := timing.remaining(s.started_at, s.max_lifetime_hours)) is not None
                          and rem <= 0)
                if gone or capped or sups[sname].gave_up:
                    if sups[sname].gave_up:
                        console.print(f"[red][{sname}] {sups[sname].health.reason}[/]")
                    elif capped:
                        console.print(f"[yellow]'{sname}' passed Colab's max lifetime cap; stopping its supervisor.[/]")
                    sups[sname].stop()
                    del sups[sname]
                    if s is not None:
                        retired.add((sname, s.started_at))
            if not active and not said_waiting:
                console.print("[dim]No active sessions; waiting. (`colabapi run` to start one.)[/]")
                said_waiting = True
            elif active:
                said_waiting = False
            time.sleep(15)
    except KeyboardInterrupt:
        pass
    finally:
        for sup in sups.values():
            sup.stop()
        console.print("[dim]Supervisor stopped. (Runtimes themselves keep running.)[/]")


# --------------------------------------------------------------------------- #
# doctor / raw
# --------------------------------------------------------------------------- #
@cli.command()
def doctor() -> None:
    """Check the environment and the official `colab` CLI interface."""
    _check_on_path()

    osname = "Windows" if plat.IS_WINDOWS else ("macOS" if plat.IS_MACOS else "Linux")
    console.print(f"platform: [cyan]{osname}[/] ({plat.default_shell_hint()})")

    ok = colab.available()
    if plat.IS_WINDOWS:
        # On Windows the `colab` binary exists but cannot run (it imports termios
        # at startup), so what matters is whether we can import and drive the
        # package in-process through our shim -- not whether the .exe is present.
        console.print(f"google-colab-cli importable: {'[green]yes[/]' if ok else '[red]no[/]'}")
        console.print("windows compatibility shim: [green]active[/] "
                      "[dim](supplies termios/tty; Google's CLI is Linux/macOS-only)[/]")
        if plat.supports_ansi():
            console.print("ANSI colours: [green]on[/]")
        elif not sys.stdout.isatty():
            # Piped or captured output disables colour by design; without the
            # qualifier this line reads as a fault when nothing is wrong.
            console.print("ANSI colours: [yellow]unavailable[/] (output is not a terminal)")
        else:
            console.print("ANSI colours: [yellow]unavailable[/]")
        console.print("registered in Windows: "
                      f"{'[green]yes[/]' if _winreg_registered() else '[yellow]no[/] (run `colabapi register`)'}")
    else:
        console.print(
            "official colab CLI: "
            + (f"[green]found[/] at {colab.path()}" if ok else "[red]not found[/]")
        )
    if not ok:
        console.print(Panel.fit(INSTALL_HINT, border_style="yellow"))
        return

    console.print(f"colab version: [cyan]{colab.version()}[/]")
    # Surface the live `new` interface so flag drift is visible.
    help_new = colab.help_text("new")
    for flag in ("--gpu", "--tpu"):
        mark = "[green]present[/]" if flag in help_new else "[yellow]NOT found, flag mapping may need update[/]"
        console.print(f"colab new {flag}: {mark}")
    console.print(f"service: {'[green]installed[/]' if service.is_installed() else '[yellow]not installed[/]'}")
    console.print("[dim]If flags differ, edit colabapi/runtime.py (colab_flags); that's the single source.[/]")


def _winreg_registered() -> bool:
    from . import winreg_install

    return winreg_install.is_registered()


# --------------------------------------------------------------------------- #
# windows registry
# --------------------------------------------------------------------------- #
@cli.command()
def register() -> None:
    """Register colabapi with Windows (Installed apps + Start menu / Win+R).

    A pipx install leaves an .exe on disk but tells Windows nothing about it, so
    colabapi never shows up in Settings -> Installed apps and cannot be launched
    from Win+R. This writes the two per-user registry keys that fix both. No
    administrator rights needed, and `colabapi unregister` reverses it.
    """
    from . import winreg_install

    if not plat.IS_WINDOWS:
        console.print("[yellow]This command only applies to Windows.[/] Nothing to do.")
        return
    try:
        exe = winreg_install.register()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not register:[/] {exc}")
        raise SystemExit(1)
    console.print(f"[green]Registered colabapi with Windows.[/] ({exe})")
    console.print("[dim]It now appears in Settings → Installed apps, and `colabapi` "
                  "works from the Start menu and Win+R.[/]")


@cli.command()
def unregister() -> None:
    """Remove colabapi's Windows registry entries (does not uninstall the package)."""
    from . import winreg_install

    if not plat.IS_WINDOWS:
        console.print("[yellow]This command only applies to Windows.[/] Nothing to do.")
        return
    winreg_install.unregister()
    console.print("[green]Removed colabapi's Windows registry entries.[/]")
    console.print("[dim]The package itself is still installed "
                  "(remove it with `pipx uninstall colabapi`).[/]")


@cli.command(context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def raw(args: tuple[str, ...]) -> None:
    """Passthrough to the official CLI: `colabapi raw -- new --gpu t4`."""
    _require_colab()
    raise SystemExit(colab.raw(list(args)))


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #
@cli.group(name="service")
def svc() -> None:
    """Install/manage the colabapi background service.

    systemd user service on Linux; a Scheduled Task on Windows.
    """


@svc.command("install")
def svc_install() -> None:
    """Install the keep-alive supervisor as a background service."""
    try:
        path = service.install(enable=True, start=False)
    except service.ServiceUnsupported as exc:
        console.print(f"[yellow]{exc}[/]")
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not install the service:[/] {exc}")
        raise SystemExit(1)
    console.print(f"[green]Installed[/] {path}")
    console.print(f"Start it with: [bold]{service.start_hint()}[/]")


@svc.command("uninstall")
def svc_uninstall() -> None:
    """Stop and remove the colabapi service."""
    try:
        service.uninstall()
    except service.ServiceUnsupported as exc:
        console.print(f"[yellow]{exc}[/]")
        raise SystemExit(1)
    console.print("[green]Removed the colabapi service.[/]")


@svc.command("status")
def svc_status() -> None:
    """Show the systemd service status."""
    console.print(service.status())


def main() -> None:
    # Windows first: when stdout is redirected, piped, or captured (a log file,
    # an IDE, CI, or the Scheduled Task the service installs), Python encodes it
    # as the legacy ANSI code page (cp1252), which cannot represent the glyphs
    # this tool prints. Force UTF-8 before Rich or anything else writes a byte;
    # errors="replace" guarantees a mangled glyph instead of a crash.
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass
    try:
        cli()
    except ColabCliNotFound as exc:
        console.print(Panel.fit(str(exc), title="Missing dependency", border_style="red"))
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
        sys.exit(130)


if __name__ == "__main__":
    main()
