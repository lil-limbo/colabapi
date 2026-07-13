"""Run the keep-alive supervisor as a detached background process, on a toggle.

`colabapi daemon` used to take over the terminal: it supervised in the
foreground and the user's shell was gone until Ctrl+C. That made the common
case hostile -- you activate the keep-alive precisely because you want to go do
other things. So the command is now a switch: the first run spawns the
supervisor detached (same `daemon --foreground` code path the systemd unit and
the Windows Scheduled Task run), says so, and returns the prompt; the second
run finds it and turns it off.

The pid file is the switch's state. It records the detached supervisor's pid in
colabapi's state dir; liveness is always re-checked through `procutil.pid_alive`
before the file is believed, so a stale file left by a reboot or a crash reads
as "off" and is cleaned up rather than blocking a fresh start.

This is deliberately separate from `colabapi service`: the service is the OS
keeping the supervisor alive across logout and reboot; this is a one-shot
background process for the current boot. The window's "Keep alive" switch
reflects and drives this toggle.
"""

from __future__ import annotations

from typing import Optional

from . import procutil
from .config import STATE_DIR, ensure_dirs

PID_FILE = STATE_DIR / "daemon.pid"


def running_pid() -> Optional[int]:
    """The live background supervisor's pid, or None. Cleans up stale files."""
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid > 0 and procutil.pid_alive(pid):
        return pid
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return None


def is_running() -> bool:
    return running_pid() is not None


def start() -> int:
    """Spawn the supervisor detached and record its pid. Returns the pid."""
    ensure_dirs()
    pid = procutil.spawn_detached(
        [procutil.python_exe(), "-m", "colabapi.cli", "daemon", "--foreground"]
    )
    PID_FILE.write_text(str(pid))
    return pid


def stop() -> Optional[int]:
    """Stop the background supervisor. Returns its pid, or None if not running.

    Only the supervisor dies. Google's keep-alive daemon it spawned is detached
    on purpose (it is what pings Colab) and keeps running until `colabapi stop`
    ends the session or Colab's own 24h daemon lifetime does.
    """
    pid = running_pid()
    if pid is None:
        return None
    procutil.kill_pid(pid)
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return pid
