"""Platform detection and per-OS directory layout.

colabapi runs on Linux, macOS and Windows. Google's official CLI does not
support Windows at all (it imports `termios` at module scope), so colabapi
supplies its own compatibility layer -- see `_winshim` and `terminal`. This
module is the single place that knows which OS we are on, so the rest of the
code never has to branch on `sys.platform` inline.

Directory choice follows each platform's convention rather than forcing XDG
everywhere: Windows users expect config under %APPDATA% and logs/state under
%LOCALAPPDATA%, and putting dotfiles in C:\\Users\\name is exactly the kind of
thing that makes a tool feel ported rather than native.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

#: Suffix for executables, so path joins work on both families.
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""


def _env_path(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else default


HOME = Path.home()


def config_dir() -> Path:
    """Where user preferences live."""
    if IS_WINDOWS:
        return _env_path("APPDATA", HOME / "AppData" / "Roaming") / "colabapi"
    return _env_path("XDG_CONFIG_HOME", HOME / ".config") / "colabapi"


def state_dir() -> Path:
    """Where session bookkeeping and logs live (machine-local, not roaming)."""
    if IS_WINDOWS:
        return _env_path("LOCALAPPDATA", HOME / "AppData" / "Local") / "colabapi" / "state"
    return _env_path("XDG_STATE_HOME", HOME / ".local" / "state") / "colabapi"


def data_dir() -> Path:
    if IS_WINDOWS:
        return _env_path("LOCALAPPDATA", HOME / "AppData" / "Local") / "colabapi" / "data"
    return _env_path("XDG_DATA_HOME", HOME / ".local" / "share") / "colabapi"


def colab_cli_state_file() -> Path:
    """Google's own session store, which we read to get a session's url + token.

    The official CLI hardcodes `~/.config/colab-cli/sessions.json` via
    os.path.expanduser on every platform, so we must not "helpfully" relocate it
    to %APPDATA% -- we have to look exactly where Google writes it.
    """
    return HOME / ".config" / "colab-cli" / "sessions.json"


def supports_ansi() -> bool:
    """True when the current stdout can render ANSI escape sequences.

    Modern Windows Terminal and PowerShell 7 do. Legacy conhost (cmd.exe on
    older builds) needs virtual-terminal processing switched on explicitly,
    which `_winshim.enable_vt_mode()` does.
    """
    if not sys.stdout.isatty():
        return False
    if not IS_WINDOWS:
        return True
    from . import _winshim

    return _winshim.enable_vt_mode()


def default_shell_hint() -> str:
    """Human-readable name of the shell we expect the user is in."""
    if IS_WINDOWS:
        return "PowerShell or CMD"
    return os.path.basename(os.environ.get("SHELL", "your shell"))
