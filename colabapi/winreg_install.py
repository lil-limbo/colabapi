"""Register colabapi with Windows so it behaves like installed software.

A pipx/pip install drops an executable somewhere and stops there. On Windows
that is not really "installed": the program does not appear in Settings ->
Installed apps, it has no entry in Add/Remove Programs, and it cannot be launched
from the Win+R box or the Start menu unless its directory happens to be on PATH.
To a Windows user that reads as a half-finished port.

Two registry keys fix both halves, and neither needs administrator rights
because we write under HKEY_CURRENT_USER (a per-user install, which is exactly
what a pipx install is):

  * ``...\\CurrentVersion\\Uninstall\\colabapi`` -- what Settings and Add/Remove
    Programs enumerate. Gives us a name, version, publisher and a working
    uninstall command.
  * ``...\\CurrentVersion\\App Paths\\colabapi.exe`` -- the mechanism Windows uses
    to resolve a bare command name typed into Win+R or the Start menu, without
    touching PATH.

Everything here is a no-op on non-Windows, and every write is reversible with
``colabapi unregister``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .platform import IS_WINDOWS

APP_NAME = "colabapi"
PUBLISHER = "lil-limbo"
HOMEPAGE = "https://github.com/lil-limbo/colabapi"

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\colabapi"
APP_PATHS_KEY = r"Software\Microsoft\Windows\CurrentVersion\App Paths\colabapi.exe"


class NotWindows(RuntimeError):
    pass


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise NotWindows("Registry registration only applies to Windows.")


def executable_path() -> Optional[str]:
    """Full path to the installed colabapi.exe, if we can find it."""
    import shutil

    found = shutil.which("colabapi")
    if found:
        return found
    guess = Path(sys.executable).parent / "colabapi.exe"
    return str(guess) if guess.is_file() else None


def _uninstall_command(exe: str) -> str:
    """A command Windows can run to actually remove colabapi.

    We report the tool that installed us rather than a generic guess: a pipx
    install lives in its own venv under pipx's home, so `pip uninstall` would
    silently do nothing and leave the user with an entry in Add/Remove Programs
    that does not work. Detect pipx by the venv layout it creates.
    """
    if "pipx" in exe.replace("/", "\\").lower():
        return 'cmd /c pipx uninstall colabapi'
    return f'cmd /c "{sys.executable}" -m pip uninstall -y colabapi'


def register() -> str:
    """Write the registry entries. Returns a human-readable summary."""
    _require_windows()
    import winreg

    exe = executable_path()
    if not exe:
        raise RuntimeError(
            "Could not locate colabapi.exe. Reinstall with:  pipx install --force colabapi"
        )
    install_dir = str(Path(exe).parent)

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "colabapi")
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, __version__)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, exe)
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, _uninstall_command(exe))
        winreg.SetValueEx(key, "URLInfoAbout", 0, winreg.REG_SZ, HOMEPAGE)
        winreg.SetValueEx(key, "HelpLink", 0, winreg.REG_SZ, HOMEPAGE)
        # We ship no repair/modify UI, so tell Windows not to offer those buttons.
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        # Settings shows this in the size column; it is in KB.
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, _install_size_kb(install_dir))

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_PATHS_KEY) as key:
        # The unnamed default value is the executable Windows should launch when
        # the user types `colabapi` into Win+R or the Start menu.
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, exe)
        winreg.SetValueEx(key, "Path", 0, winreg.REG_SZ, install_dir)

    return exe


def unregister() -> None:
    """Remove the registry entries. Safe to call when they are absent."""
    _require_windows()
    import winreg

    for key in (UNINSTALL_KEY, APP_PATHS_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            pass


def is_registered() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY):
            return True
    except FileNotFoundError:
        return False


def _install_size_kb(install_dir: str) -> int:
    """Best-effort install size for the Settings listing."""
    try:
        total = sum(
            f.stat().st_size for f in Path(install_dir).rglob("*") if f.is_file()
        )
        return max(total // 1024, 1)
    except OSError:
        return 1
