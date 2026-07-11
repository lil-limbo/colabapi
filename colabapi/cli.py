"""colabapi command-line interface.

colabapi wraps Google's official `colab` CLI, adding a single command, a systemd
service, a runtime picker, a live resource monitor, and session-time display.
Authentication, runtime allocation, and the interactive terminal are delegated to
Google's own tool (the ban-safe path); this module is the friendly orchestration
layer on top.
"""

from __future__ import annotations

import sys
import time

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import __version__, monitor as monitor_mod, runtime as rt, service, shellview, timing, ui
from .colabcli import ColabCLI, ColabCliNotFound, INSTALL_HINT
from .config import Config, Session, SessionStore, ensure_dirs
from .keepalive import KeepAlive

console = Console()
colab = ColabCLI()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _require_colab() -> None:
    if not colab.available():
        console.print(Panel.fit(INSTALL_HINT, title="Missing dependency", border_style="red"))
        raise SystemExit(1)


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
    console.print("[dim]Tip: `colabapi service install` keeps it alive after you log out of this box.[/]")


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
@click.option("--foreground", is_flag=True, help="Run in the foreground (used by systemd).")
def daemon(name: str | None, foreground: bool) -> None:
    """Supervisory keep-alive: verify the runtime stays reachable, with backoff.

    Google's official CLI runs the primary keep-alive against Colab's own tunnel
    endpoint; this daemon is a belt-and-suspenders health check that alerts (via
    the service log) if that stops working, and it never hammers reconnects.
    """
    _require_colab()
    s = _pick_session("supervise", name)
    console.print(f"[cyan]colabapi keep-alive supervisor starting for '{s.name}'[/]")

    ka = KeepAlive(_run_remote, interval=Config.load().keepalive_interval,
                   on_error=lambda e: console.print(f"[dim]keep-alive check failed: {e}[/]"))
    ka.start()
    try:
        while True:
            rem = timing.remaining(s.started_at, s.max_lifetime_hours)
            if rem is not None and rem <= 0:
                console.print("[yellow]Past estimated max lifetime; stopping supervisor.[/]")
                break
            if ka.failures >= 5:
                console.print("[red]Runtime unreachable for several checks; session likely ended.[/]")
                break
            time.sleep(15)
            if not foreground:
                break
    except KeyboardInterrupt:
        pass
    finally:
        ka.stop()
        console.print("[dim]Keep-alive supervisor stopped.[/]")


# --------------------------------------------------------------------------- #
# doctor / raw
# --------------------------------------------------------------------------- #
@cli.command()
def doctor() -> None:
    """Check the environment and the official `colab` CLI interface."""
    ok = colab.available()
    console.print(f"official colab CLI: {'[green]found[/] at ' + colab.path() if ok else '[red]not found[/]'}")
    if not ok:
        console.print(Panel.fit(INSTALL_HINT, border_style="yellow"))
        return
    console.print(f"colab version: [cyan]{colab.version()}[/]")
    # Surface the live `new` interface so flag drift is visible.
    help_new = colab.help_text("new")
    for flag in ("--gpu", "--tpu"):
        mark = "[green]present[/]" if flag in help_new else "[yellow]NOT found, flag mapping may need update[/]"
        console.print(f"colab new {flag}: {mark}")
    console.print("[dim]If flags differ, edit colabapi/runtime.py (colab_flags); that's the single source.[/]")


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
    """Install/manage the colabapi systemd user service."""


@svc.command("install")
def svc_install() -> None:
    """Install and enable the keep-alive supervisor as a systemd user service."""
    path = service.install(enable=True, start=False)
    console.print(f"[green]Installed[/] {path}")
    console.print("Start it with: [bold]systemctl --user start colabapi[/]")


@svc.command("uninstall")
def svc_uninstall() -> None:
    """Stop and remove the colabapi systemd service."""
    service.uninstall()
    console.print("[green]Removed the colabapi service.[/]")


@svc.command("status")
def svc_status() -> None:
    """Show the systemd service status."""
    console.print(service.status())


def main() -> None:
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
