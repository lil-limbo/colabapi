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

Registration also makes colabapi look and open like an app, not a loose exe:

  * The bundled logo is copied to a stable per-user path and used as the
    ``DisplayIcon``, so the Installed-apps entry shows colabapi's own mark
    instead of a generic executable icon. (Stable copy rather than a path into
    the venv: pipx reinstalls recreate the venv, and a registry value pointing
    into a deleted venv means a blank icon.)
  * A Start-menu shortcut launches the graphical window (``colabapi ui``), so
    clicking "colabapi" in the app list opens a window, which is what clicking
    an app in the app list is universally expected to do.

Everything here is a no-op on non-Windows, and every write is reversible with
``colabapi unregister``.
"""

from __future__ import annotations

import os
import subprocess
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


def _icon_dir() -> Path:
    """Per-user home for the copied icon, outside any venv (see module doc)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def _install_icon() -> Optional[str]:
    """Copy the bundled .ico to its stable path. Best-effort: no icon, no crash."""
    try:
        from importlib.resources import files

        src = files("colabapi").joinpath("assets/colabapi.ico")
        if not src.is_file():
            return None
        dest = _icon_dir() / "colabapi.ico"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        return str(dest)
    except (OSError, ModuleNotFoundError):
        return None


def _shortcut_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "colabapi.lnk"


def _create_start_menu_shortcut(exe: str, icon: Optional[str]) -> bool:
    """Create the Start-menu shortcut that opens `colabapi ui`.

    .lnk files have no text format; the supported way to write one without
    pywin32 is the WScript.Shell COM object, driven through PowerShell (present
    on every Windows this tool supports). Prefer pythonw.exe -m colabapi ui so
    the window opens without a console flashing up behind it; fall back to the
    console exe when pythonw is not in the venv. Best-effort by design: a
    missing shortcut must not fail registration.
    """
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.is_file():
        target, args = str(pythonw), "-m colabapi ui"
    else:
        target, args = exe, "ui"

    lnk = _shortcut_path()
    icon_line = f"$s.IconLocation = '{icon}';" if icon else ""
    script = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$s = $ws.CreateShortcut('{lnk}');"
        f"$s.TargetPath = '{target}';"
        f"$s.Arguments = '{args}';"
        f"$s.WorkingDirectory = '{Path(exe).parent}';"
        "$s.Description = 'Run and keep a Google Colab runtime alive';"
        f"{icon_line}"
        "$s.Save()"
    )
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0 and lnk.is_file()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _uninstall_command(exe: str) -> str:
    """A command Windows can run to actually remove colabapi.

    Two hard-won rules live here, both learned from a real machine that this
    function left in a corrupted state:

    * **Detect pipx from ``sys.executable``, never from ``exe``.** ``exe`` is
      the shim Windows launches (``~\\.local\\bin\\colabapi.exe``) and its path
      never contains "pipx"; the pipx-ness is in the interpreter's path, which
      lives inside ``pipx\\venvs\\colabapi``. Checking ``exe`` sent every pipx
      install -- the recommended install -- down the ``pip uninstall`` branch,
      which gutted the venv while leaving pipx's metadata and shim behind, so
      ``pipx list`` errored and even ``pipx uninstall`` could no longer clean up.

    * **Unregister first, while the exe still exists, then remove the package.**
      Neither ``pipx uninstall`` nor ``pip uninstall`` touches the registry, so
      without the leading ``unregister`` the Settings entry and the App Paths
      key survive their own uninstall -- a dead "colabapi" row in Installed apps
      pointing at nothing. ``&&`` (cmd's conditional chain; this string is run
      by cmd, not PowerShell) also means a failed unregister leaves the package
      installed and the entry functional, so the user can simply retry, instead
      of removing the package and stranding the entry.

    ``cmd /s /c`` with the whole command in one outer quote pair is the one
    quoting form cmd.exe treats predictably when the inner paths contain both
    spaces and their own quotes.
    """
    if "pipx" in (sys.executable or "").replace("/", "\\").lower():
        inner = f'"{exe}" unregister && pipx uninstall colabapi'
    else:
        inner = f'"{exe}" unregister && "{sys.executable}" -m pip uninstall -y colabapi'
    return f'cmd /s /c "{inner}"'


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
    icon = _install_icon()

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "colabapi")
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, __version__)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, icon or exe)
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

    # Clicking "colabapi" in the Start menu / app list should open the window.
    _create_start_menu_shortcut(exe, icon)

    return exe


def unregister() -> None:
    """Remove the registry entries, shortcut and copied icon. Safe when absent."""
    _require_windows()
    import winreg

    for key in (UNINSTALL_KEY, APP_PATHS_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            pass

    try:
        _shortcut_path().unlink(missing_ok=True)
    except OSError:
        pass
    try:
        icon = _icon_dir() / "colabapi.ico"
        icon.unlink(missing_ok=True)
        # Remove the directory too when the icon was its only tenant.
        icon.parent.rmdir()
    except OSError:
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
