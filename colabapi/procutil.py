"""Cross-platform process liveness and spawning.

Kept separate because the naive approach is actively dangerous on Windows:
`os.kill(pid, 0)` is the standard POSIX "is this pid alive?" probe, but Python on
Windows implements os.kill for any signal other than CTRL_C_EVENT/CTRL_BREAK_EVENT
by calling TerminateProcess -- so the usual liveness check would *kill* the very
keep-alive daemon it was checking on. We use the Win32 API instead.

The POSIX side has its own trap: a child we spawned and never waited on becomes
a ZOMBIE when it exits, and `os.kill(pid, 0)` succeeds on zombies. The
long-running supervisor is exactly the process that hits this -- it spawns the
keep-alive daemon, the daemon exits 24h later, the zombie sits in our process
table, the liveness probe keeps saying "alive", and the supervisor never
respawns while the runtime quietly idles out. So we keep every Popen we create
and poll() (i.e. reap) it before trusting os.kill.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Optional, Sequence

from .platform import IS_WINDOWS

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Children spawned by THIS process, so pid_alive can reap them (see module
# docstring). Guarded by a lock: the keep-alive supervisor thread and the CLI
# thread can both be in here.
_children: dict[int, subprocess.Popen] = {}
_children_lock = threading.Lock()

_k32 = None


def _kernel32():
    """kernel32 with proper signatures, created once.

    Declaring argtypes/restype is not pedantry on 64-bit Windows: ctypes'
    default return type is a 32-bit c_int, so a HANDLE (pointer-sized) coming
    back from OpenProcess would be truncated, and passing it on to
    GetExitCodeProcess/CloseHandle as a default-converted int is undefined for
    values that do not survive the round-trip. Windows handles are 32-bit
    *significant*, but the sign-extension contract is exactly the kind of thing
    that should be encoded once here rather than relied on implicitly.
    """
    global _k32
    if _k32 is not None:
        return _k32
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    _k32 = k32
    return k32


def _reap_if_ours(pid: int) -> Optional[bool]:
    """poll() a child we spawned. True=still running, False=exited, None=not ours."""
    with _children_lock:
        proc = _children.get(pid)
        if proc is None:
            return None
        if proc.poll() is None:
            return True
        # Exited and now reaped; forget it so the dict cannot grow unbounded
        # (and so a recycled pid is not mistaken for our old child).
        del _children[pid]
        return False


def pid_alive(pid: Optional[int]) -> bool:
    """True if `pid` names a running process."""
    if not pid or pid <= 0:
        return False

    ours = _reap_if_ours(pid)
    if ours is not None:
        return ours

    if IS_WINDOWS:
        import ctypes

        k32 = _kernel32()
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)

    # A zombie left by some OTHER process still answers os.kill(pid, 0); on
    # Linux /proc tells the truth. (Zombies from our own spawns are handled by
    # _reap_if_ours above; this covers e.g. a daemon pid recorded by a previous
    # colabapi process whose parent has not reaped it.)
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                stat = f.read()
            # Field 3 is the state, after the parenthesised (and possibly
            # space-containing) comm field.
            state = stat.rsplit(b")", 1)[1].split()[0]
            return state != b"Z"
        except (OSError, IndexError):
            pass  # /proc unreadable: fall back to the kill(0) probe below

    try:
        os.kill(pid, 0)  # signal 0: check only, never delivered
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def kill_pid(pid: Optional[int]) -> None:
    if not pid_alive(pid):
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, check=False)
        return
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def spawn_detached(args: Sequence[str], env: Optional[dict] = None) -> int:
    """Start a background process that outlives this one. Returns its pid.

    Detaching matters here: the keep-alive must survive the terminal that
    started it closing. On POSIX that means a new session (no controlling tty,
    so no SIGHUP); on Windows it means DETACHED_PROCESS plus a new process group,
    so a Ctrl+C in the parent console is not broadcast to it.
    """
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "env": env or os.environ.copy(),
    }
    if IS_WINDOWS:
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        # CREATE_NO_WINDOW stops a console window flashing up on the desktop
        # every time the daemon is (re)started.
        kwargs["creationflags"] = (
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(list(args), **kwargs)
    # Keep the handle: if this process outlives the child (the supervisor does,
    # by design), the child must be reaped or its zombie will read as alive
    # forever. See pid_alive.
    with _children_lock:
        _children[proc.pid] = proc
    return proc.pid


def python_exe() -> str:
    """The interpreter to re-invoke ourselves with.

    On Windows prefer pythonw.exe for detached children so no console window
    appears; fall back to python.exe if it is not alongside.
    """
    exe = sys.executable or "python"
    if IS_WINDOWS:
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pythonw):
            return pythonw
    return exe
