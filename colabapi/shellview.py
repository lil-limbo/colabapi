"""Open an interactive Colab shell with a live resource monitor above it.

When tmux is available locally we build a split window: a small top pane runs the
colabapi monitor for the chosen session, and the bottom pane runs the real
interactive shell (`colab console -s <name>`). The session name is shown in the
tmux status bar. When the shell exits, the whole layout tears down.

If tmux is not installed we fall back to printing a one-shot monitor snapshot,
then handing the terminal straight to `colab console`.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

from rich.console import Console

from .colabcli import ColabCLI

console = Console()

# Height (rows) of the monitor pane on top.
_MONITOR_ROWS = 11


def _child_env(colab: ColabCLI) -> dict:
    return colab._child_env()


def open_shell(colab: ColabCLI, name: str) -> int:
    """Open the shell for session `name`, with a live monitor on top if possible."""
    colab.session = name
    if shutil.which("tmux"):
        return _open_tmux(colab, name)
    return _open_plain(colab, name)


def _open_tmux(colab: ColabCLI, name: str) -> int:
    exe = colab.require()
    py = sys.executable or "python3"
    tmux_ses = f"colabapi_{name}_{os.getpid()}"

    # Env is embedded directly in each pane command, because tmux panes do not
    # reliably inherit the environment of the client that creates the session.
    env_prefix = (
        f"COLABAPI_COLAB_BIN={shlex.quote(exe)} "
        f"OAUTHLIB_RELAX_TOKEN_SCOPE=1 "
    )
    monitor_cmd = env_prefix + f"{shlex.quote(py)} -m colabapi.cli _panemonitor {shlex.quote(name)}"
    # When the shell exits, kill the whole tmux session so `attach` returns and we
    # clean up (otherwise the monitor pane would keep the session alive).
    console_cmd = (
        env_prefix
        + f"{shlex.quote(exe)} console -s {shlex.quote(name)}; "
        + f"tmux kill-session -t {shlex.quote(tmux_ses)} 2>/dev/null"
    )

    env = _child_env(colab)

    def tmux(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", *args], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Top pane: the live monitor. Window name = session name.
    tmux("new-session", "-d", "-s", tmux_ses, "-n", name, monitor_cmd)
    # Bottom pane: the interactive shell.
    tmux("split-window", "-v", "-t", tmux_ses, console_cmd)
    # Keep the monitor pane small and focus the shell.
    tmux("resize-pane", "-t", f"{tmux_ses}.0", "-y", str(_MONITOR_ROWS))
    tmux("select-pane", "-t", f"{tmux_ses}.1")
    # A distinct prefix (Ctrl-a) avoids clashing with the remote tmux's Ctrl-b.
    tmux("set-option", "-t", tmux_ses, "prefix", "C-a")
    tmux("set-option", "-t", tmux_ses, "status", "on")
    tmux("set-option", "-t", tmux_ses, "status-style", "bg=colour24,fg=white")
    tmux("set-option", "-t", tmux_ses, "status-left", f" colabapi ❯ {name} ")
    tmux("set-option", "-t", tmux_ses, "status-left-length", "48")
    tmux("set-option", "-t", tmux_ses, "status-right", " Ctrl-a d to detach ")

    console.print(f"[green]Opening[/] [cyan]{name}[/]: monitor on top, shell below. "
                  "[dim](tmux prefix is Ctrl-a; type 'exit' to close)[/]")
    code = subprocess.run(["tmux", "attach-session", "-t", tmux_ses], env=env).returncode
    tmux("kill-session", "-t", tmux_ses)  # no-op if already gone
    console.print(f"[dim]Shell closed. Session '{name}' is still running "
                  "(use `colabapi stop` to end it).[/]")
    return code


def _open_plain(colab: ColabCLI, name: str) -> int:
    """Fallback without tmux: print a monitor snapshot, then hand over the shell."""
    from . import monitor as monitor_mod

    def run_remote(code: str) -> str:
        res = colab.exec_code(code)
        return res.stdout if res.stdout.strip() else res.stderr

    console.print("[dim]tmux not found; showing a one-shot monitor snapshot, then the shell.[/]")
    try:
        console.print(monitor_mod.build_panel(run_remote, f"session {name}"))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[dim](could not read monitor: {exc})[/]")
    console.print(f"[green]Terminal into[/] [cyan]{name}[/]. Type 'exit' or Ctrl-D to return.\n")
    code = colab.console()
    console.print(f"\n[dim]Shell closed. Session '{name}' is still running.[/]")
    return code
