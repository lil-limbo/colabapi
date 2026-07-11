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

from . import __version__, monitor as monitor_mod, runtime as rt, service, timing
from .colabcli import ColabCLI, ColabCliNotFound, INSTALL_HINT
from .config import Config, Session, ensure_dirs
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


def _session_or_exit() -> Session:
    s = Session.load()
    if not s or not s.is_active:
        console.print("[red]No active runtime.[/] Start one with [bold]colabapi run[/].")
        raise SystemExit(1)
    return s


def _run_remote(cmd: str) -> str:
    res = colab.exec(cmd)
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
        "colabapi will hand off to Google's official sign-in.\n\n"
        "A browser window opens for [bold]Google's own login[/] — including any\n"
        "2FA or device checks, exactly as normal. [bold]Your password never\n"
        "touches colabapi.[/]",
        title="Sign in to Colab", border_style="cyan"))
    code = colab.auth()
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
            f"[yellow]needs {r.tier.upper()}[/] — not on a free account"
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

    if not colab.is_authenticated():
        console.print("[yellow]Not signed in.[/] Running sign-in first…")
        if colab.auth() != 0:
            console.print("[red]Sign-in failed.[/]")
            raise SystemExit(1)

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

    console.print(f"Requesting [cyan]{chosen.label}[/] via [bold]colab new {' '.join(chosen.colab_flags)}[/]…")
    res = colab.new_runtime(chosen.colab_flags)
    if res.stdout.strip():
        console.print(res.stdout.strip())
    if not res.ok:
        console.print(f"[red]colab new failed (exit {res.returncode}).[/]")
        if res.stderr.strip():
            console.print(f"[dim]{res.stderr.strip()}[/]")
        raise SystemExit(1)

    s = Session(runtime=chosen.key, started_at=time.time(),
                max_lifetime_hours=_max_lifetime_for(chosen.key))
    s.save()
    cfg.default_runtime = chosen.key
    cfg.save()

    console.print(f"[green]Runtime ready.[/] {timing.session_line(s.started_at, s.max_lifetime_hours)}")
    console.print("\nNow: [bold]colabapi shell[/] · [bold]colabapi monitor[/] · [bold]colabapi status[/]")
    console.print("[dim]Tip: `colabapi service install` keeps it alive after you log out of this box.[/]")


# --------------------------------------------------------------------------- #
# shell / repl
# --------------------------------------------------------------------------- #
@cli.command()
def shell() -> None:
    """Open an interactive terminal on the runtime (`colab console`)."""
    _require_colab()
    s = _session_or_exit()
    console.print(f"[green]Terminal into Colab[/] — {timing.session_line(s.started_at, s.max_lifetime_hours)}")
    console.print("[dim]Type 'exit' or press Ctrl-D to return here. Ctrl-C is passed to the runtime.[/]\n")
    code = colab.console()
    console.print(f"\n[dim]Terminal closed (exit {code}). The Colab runtime is still running.[/]")


@cli.command()
def repl() -> None:
    """Open an interactive Python REPL on the runtime (`colab repl`)."""
    _require_colab()
    _session_or_exit()
    colab.repl()


# --------------------------------------------------------------------------- #
# monitor
# --------------------------------------------------------------------------- #
@cli.command()
def monitor() -> None:
    """Live CPU / RAM / GPU monitor for the runtime (Ctrl-C to exit)."""
    _require_colab()
    s = _session_or_exit()

    def line() -> str:
        return timing.session_line(s.started_at, s.max_lifetime_hours)

    monitor_mod.live_monitor(_run_remote, line, interval=Config.load().monitor_interval)
    console.print("[dim]Monitor closed.[/]")


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
@cli.command()
def status() -> None:
    """Show the current session, reachability, and estimated time remaining."""
    s = Session.load()
    if not s or not s.is_active:
        console.print("No active runtime. Run [bold]colabapi run[/] to start.")
        return
    table = Table(show_header=False, box=None)
    table.add_row("Runtime", f"[cyan]{s.runtime}[/]")
    table.add_row("Uptime / est.", timing.session_line(s.started_at, s.max_lifetime_hours))
    if colab.available():
        res = colab.status()
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
def stop() -> None:
    """Forget the local session. (The Colab runtime ends on its own timers or via the Colab UI.)"""
    if not Session.load():
        console.print("Nothing to stop.")
        return
    Session.clear()
    console.print("[green]Local session cleared.[/]")
    console.print("[dim]To actually terminate the runtime now, use the Colab UI "
                  "(Runtime → Disconnect and delete runtime) or `colabapi raw -- <teardown-cmd>`.[/]")


# --------------------------------------------------------------------------- #
# daemon (systemd)
# --------------------------------------------------------------------------- #
@cli.command()
@click.option("--foreground", is_flag=True, help="Run in the foreground (used by systemd).")
def daemon(foreground: bool) -> None:
    """Supervisory keep-alive: verify the runtime stays reachable, with backoff.

    Google's official CLI runs the primary keep-alive against Colab's own tunnel
    endpoint; this daemon is a belt-and-suspenders health check that alerts (via
    the service log) if that stops working, and it never hammers reconnects.
    """
    _require_colab()
    s = _session_or_exit()
    console.print("[cyan]colabapi keep-alive supervisor starting[/]")

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
        mark = "[green]present[/]" if flag in help_new else "[yellow]NOT found — flag mapping may need update[/]"
        console.print(f"colab new {flag}: {mark}")
    console.print("[dim]If flags differ, edit colabapi/runtime.py (colab_flags) — that's the single source.[/]")


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
