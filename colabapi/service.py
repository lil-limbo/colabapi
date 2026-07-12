"""Run the colabapi supervisor as a background service.

The point of the service is the one thing Google's CLI cannot do for you: its
keep-alive daemon is a detached child of your *terminal*, so it dies when the
machine sleeps, logs out or reboots -- and the runtime then idles out even though
nothing was wrong with it. Registering colabapi with the OS means the supervisor
comes back automatically, and the session survives you closing the laptop.

Two backends, one interface:

  * Linux  -- a systemd **user** unit (no root), with lingering enabled so it
    keeps running after you disconnect from a VPS.
  * Windows -- a **Scheduled Task** that runs at logon. This is the native way to
    do "start this in the background for this user, forever"; it needs no admin
    rights, unlike installing a true Windows Service, and it survives reboots.

macOS has neither, so `install()` says so plainly instead of pretending.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .platform import IS_LINUX, IS_WINDOWS, EXE_SUFFIX

UNIT_NAME = "colabapi.service"
USER_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

#: Name of the Windows Scheduled Task, as it appears in Task Scheduler.
TASK_NAME = "colabapi"

_UNIT_TEMPLATE = """\
[Unit]
Description=colabapi: persistent Google Colab runtime keep-alive supervisor
Documentation=https://github.com/lil-limbo/colabapi
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_path} daemon --foreground
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


class ServiceUnsupported(RuntimeError):
    """No service backend exists on this platform."""


def _colabapi_path() -> str:
    """Absolute path to the installed `colabapi` command."""
    found = shutil.which("colabapi")
    if found:
        return found
    # pipx/venv installs may not be on PATH (notably as root); fall back to the
    # script that sits next to the interpreter running us.
    import sys

    guess = Path(sys.executable).parent / f"colabapi{EXE_SUFFIX}"
    if guess.is_file():
        return str(guess)
    return str(Path.home() / ".local" / "bin" / "colabapi")


# --------------------------------------------------------------------------- #
# systemd (Linux)
# --------------------------------------------------------------------------- #
def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _install_systemd(enable: bool, start: bool) -> str:
    USER_UNIT_DIR.mkdir(parents=True, exist_ok=True)
    unit_path = USER_UNIT_DIR / UNIT_NAME
    unit_path.write_text(_UNIT_TEMPLATE.format(exec_path=_colabapi_path()))
    _systemctl("daemon-reload")
    if enable:
        _systemctl("enable", UNIT_NAME)
    if start:
        _systemctl("start", UNIT_NAME)
    # Lingering is what lets a *user* service keep running once you log out of
    # the VPS -- without it systemd tears down the user manager on logout.
    # getpass.getuser() falls back to the pw database when $USER is unset
    # (cron, some sudo configurations); an empty argument would silently fail.
    import getpass

    try:
        user = getpass.getuser()
    except OSError:
        user = os.environ.get("USER", "")
    if user:
        subprocess.run(["loginctl", "enable-linger", user],
                       capture_output=True, text=True)
    return str(unit_path)


# --------------------------------------------------------------------------- #
# Scheduled Task (Windows)
# --------------------------------------------------------------------------- #
def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True)


def _install_schtask(start: bool) -> str:
    # /SC ONLOGON  -- start when this user logs in, so it survives reboots.
    # /RL LIMITED  -- run with normal user rights; no admin prompt, and the task
    #                 can read the user's own Colab credentials.
    # /F           -- replace an existing task instead of failing.
    command = f'"{_colabapi_path()}" daemon --foreground'
    result = _schtasks(
        "/Create", "/TN", TASK_NAME, "/TR", command,
        "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
    )
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout or "schtasks failed").strip()
        )
    if start:
        _schtasks("/Run", "/TN", TASK_NAME)
    return f"Scheduled Task '{TASK_NAME}'"


# --------------------------------------------------------------------------- #
# public interface
# --------------------------------------------------------------------------- #
def install(enable: bool = True, start: bool = False) -> str:
    if IS_WINDOWS:
        return _install_schtask(start=start)
    if IS_LINUX:
        return _install_systemd(enable=enable, start=start)
    raise ServiceUnsupported(
        "colabapi has no background service on macOS yet (systemd is Linux-only "
        "and launchd support is not written). The keep-alive still runs while "
        "`colabapi daemon` is open."
    )


def uninstall() -> None:
    if IS_WINDOWS:
        _schtasks("/Delete", "/TN", TASK_NAME, "/F")
        return
    if IS_LINUX:
        _systemctl("disable", "--now", UNIT_NAME)
        (USER_UNIT_DIR / UNIT_NAME).unlink(missing_ok=True)
        _systemctl("daemon-reload")
        return
    raise ServiceUnsupported("Nothing to uninstall on this platform.")


def status() -> str:
    if IS_WINDOWS:
        result = _schtasks("/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST")
        if result.returncode != 0:
            return "colabapi service not installed (no Scheduled Task)."
        return result.stdout
    if IS_LINUX:
        return _systemctl("status", UNIT_NAME).stdout or "colabapi service not installed"
    return "No service backend on this platform."


def is_installed() -> bool:
    if IS_WINDOWS:
        return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0
    if IS_LINUX:
        return (USER_UNIT_DIR / UNIT_NAME).exists()
    return False


def start_hint() -> str:
    """The command the user should run next to actually start the service."""
    if IS_WINDOWS:
        return f'schtasks /Run /TN {TASK_NAME}'
    return "systemctl --user start colabapi"
