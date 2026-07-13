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
from .platform import IS_WINDOWS

console = Console()

# Height (rows) of the monitor pane on top.
_MONITOR_ROWS = 11


def _child_env(colab: ColabCLI) -> dict:
    return colab._child_env()


def open_shell(colab: ColabCLI, name: str) -> int:
    """Open the shell for session `name`, with a live monitor on top if possible."""
    colab.session = name
    # Only split on Linux/macOS. A `tmux` on Windows means WSL or Git-Bash, whose
    # tmux cannot drive the Windows console we are actually attached to, so the
    # split would half-render; the plain view is correct there.
    if not IS_WINDOWS and shutil.which("tmux"):
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
    # Run colabapi's own resilient console (terminal.py) in the pane rather than
    # `colab console`: Google's has no keepalive ping and no reconnect, so a blip
    # would kill the shell inside the split and tear the whole layout down.
    # When the shell exits, kill the tmux session so `attach` returns and we clean
    # up (otherwise the monitor pane would keep the session alive forever).
    console_cmd = (
        env_prefix
        + f"{shlex.quote(py)} -m colabapi.cli _paneconsole {shlex.quote(name)}; "
        + f"tmux kill-session -t {shlex.quote(tmux_ses)} 2>/dev/null"
    )

    env = _child_env(colab)

    def tmux(*args: str) -> int:
        return subprocess.run(["tmux", *args], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode

    # The SHELL founds the session, not the monitor. The founding pane is the
    # one the whole tmux session lives or dies with, and the shell is what the
    # user came for -- with the monitor as the founding pane (as in 0.2.2), a
    # monitor that exited early took the layout down or left the shell filling
    # the terminal with no monitor at all, seemingly at random.
    if tmux("new-session", "-d", "-s", tmux_ses, "-n", name, console_cmd) != 0:
        # tmux refused (nested session, broken server): the plain view still works.
        return _open_plain(colab, name)
    # Monitor above the shell: -b puts the new pane before (above) the target,
    # -l fixes its height, -d keeps focus on the shell. If this split fails the
    # shell simply opens full-height rather than failing the whole command.
    tmux("split-window", "-v", "-b", "-d", "-l", str(_MONITOR_ROWS),
         "-t", f"{tmux_ses}.0", monitor_cmd)
    # A distinct prefix (Ctrl-a) avoids clashing with the remote tmux's Ctrl-b.
    tmux("set-option", "-t", tmux_ses, "prefix", "C-a")
    tmux("set-option", "-t", tmux_ses, "status", "on")
    tmux("set-option", "-t", tmux_ses, "status-style", "bg=colour24,fg=white")
    tmux("set-option", "-t", tmux_ses, "status-left", f" colabapi ❯ {name} ")
    tmux("set-option", "-t", tmux_ses, "status-left-length", "48")
    tmux("set-option", "-t", tmux_ses, "status-right", " type: exit to Quit shell ")
    tmux("set-option", "-t", tmux_ses, "status-right-length", "28")

    console.print(f"[green]Opening[/] [cyan]{name}[/]: monitor on top, shell below. "
                  "[dim](type 'exit' to quit the shell)[/]")
    code = subprocess.run(["tmux", "attach-session", "-t", tmux_ses], env=env).returncode
    tmux("kill-session", "-t", tmux_ses)  # no-op if already gone
    return code


def _open_plain(colab: ColabCLI, name: str) -> int:
    """No local split: print a monitor snapshot, then hand over the shell.

    This is the normal path on Windows (where the tmux split does not apply) and
    the fallback anywhere tmux is missing. The shell itself is identical -- the
    same resilient, reconnecting client -- so only the live monitor pane is lost,
    and `colabapi monitor` still gives that in another window.
    """
    from . import monitor as monitor_mod, persist

    def run_remote(code: str) -> str:
        res = colab.exec_code(code)
        return res.stdout if res.stdout.strip() else res.stderr

    if not IS_WINDOWS:
        console.print("[dim]tmux not found locally; showing a snapshot, then the shell.[/]")
    try:
        console.print(monitor_mod.build_panel(run_remote, f"session {name}"))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[dim](could not read monitor: {exc})[/]")
    console.print(f"[green]Terminal into[/] [cyan]{name}[/]. Type 'exit' to return, "
                  "or press [bold]Ctrl+][/] to detach and leave it running.")
    console.print(f"[dim]{persist.detach_hint(name)}[/]\n")
    return colab.console()
