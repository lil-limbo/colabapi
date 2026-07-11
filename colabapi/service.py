"""systemd user-service integration.

`colabapi service install` drops a user-level systemd unit that runs the
keep-alive/tunnel daemon so the session survives after you log out of the VPS.
User-level (systemctl --user) is preferred: it needs no root and keeps colabapi
scoped to the invoking user. A --system flag is offered for headless servers
where lingering user services are undesirable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

UNIT_NAME = "colabapi.service"
USER_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

_UNIT_TEMPLATE = """\
[Unit]
Description=colabapi — persistent Google Colab runtime tunnel + keep-alive
Documentation=https://github.com/lil-limbo/colabapi
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_path} daemon --foreground
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _colabapi_path() -> str:
    return shutil.which("colabapi") or str(Path.home() / ".local" / "bin" / "colabapi")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def install(enable: bool = True, start: bool = False) -> str:
    USER_UNIT_DIR.mkdir(parents=True, exist_ok=True)
    unit_path = USER_UNIT_DIR / UNIT_NAME
    unit_path.write_text(_UNIT_TEMPLATE.format(exec_path=_colabapi_path()))
    _systemctl("daemon-reload")
    if enable:
        _systemctl("enable", UNIT_NAME)
    if start:
        _systemctl("start", UNIT_NAME)
    # Enable lingering so the user service runs without an active login session.
    subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")],
                   capture_output=True, text=True)
    return str(unit_path)


def uninstall() -> None:
    _systemctl("disable", "--now", UNIT_NAME)
    unit_path = USER_UNIT_DIR / UNIT_NAME
    unit_path.unlink(missing_ok=True)
    _systemctl("daemon-reload")


def status() -> str:
    return _systemctl("status", UNIT_NAME).stdout or "colabapi service not installed"


def is_installed() -> bool:
    return (USER_UNIT_DIR / UNIT_NAME).exists()
