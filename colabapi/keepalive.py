"""Keep the runtime alive, and keep the thing that keeps it alive alive.

Google's official CLI already ships the correct keep-alive: a hidden
`colab keep-alive <endpoint> <session>` daemon that issues

    GET https://colab.research.google.com/tun/m/<endpoint>/keep-alive/
    X-Colab-Tunnel: Google

once every 60 seconds against Colab's Tunnel Frontend. That is the sanctioned,
first-party way to hold off the idle timeout, so colabapi does not invent its own
scheme -- we run *Google's* daemon, through Google's own code path, with Google's
own credentials. (An earlier version of colabapi pinged the runtime by executing
Python on it every minute. That was heavier, and it synthesised activity on the
VM rather than signalling liveness to the frontend. This is both lighter and more
clearly legitimate.)

What Google's design does not survive, and what this module adds:

  * **The daemon dies with the user's machine.** `colab new` spawns the
    keep-alive as a detached child *on the laptop*. Suspend the laptop, close the
    lid, log out of the VPS, and the pings stop -- so the runtime idles out even
    though nothing was wrong with it. colabapi's whole premise is a session that
    outlives your terminal, so we supervise the daemon and re-spawn it whenever it
    is gone, and (via `colabapi service install`) we run that supervisor under
    systemd or a Windows Scheduled Task so it survives logout and reboot.
  * **The daemon deliberately exits after 24 hours** (`max_duration = 24 * 3600`,
    to avoid zombie processes) and after two consecutive 4xx responses. The 24h
    exit is a lifetime guard, not a signal that the session is over, so we simply
    start a fresh one.
  * **On Windows the daemon cannot start at all.** `spawn_keep_alive` launches
    `python -m colab_cli.cli`, which raises ImportError on `termios` before it
    parses a single argument. We launch it through `colabapi._colab_shim`
    instead, which installs the compatibility layer first (see `_winshim`).

What this does NOT do, on purpose: it does not fake user activity, hold an idle
GPU to reserve it, or try to defeat Colab's limits. The idle timeout is held off
by the ping Google themselves ship; the absolute max lifetime (~12h free, ~24h
paid) and GPU quota lockouts are hard, server-side caps that no client can move,
and `daemon` reports them plainly rather than retrying into a wall.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import procutil

# The official CLI accepts global `--auth` / `--config` flags, and `colab new`
# propagates them into the daemon it spawns (see their spawn_keep_alive). A
# daemon respawned WITHOUT them would fall back to Typer's defaults -- oauth2
# auth and the default sessions.json -- which for an ADC user or a custom
# --config path means a daemon that either cannot authenticate or cannot find
# its session, and a runtime that idles out while the supervisor reports
# success. colabapi itself always uses the defaults, so these are opt-in env
# vars for users who created their session with non-default flags.
AUTH_ENV = "COLABAPI_COLAB_AUTH"
CONFIG_ENV = "COLABAPI_COLAB_CONFIG"

# How often the supervisor checks that the daemon is still breathing. This is
# not the ping interval -- Google's daemon pings every 60s on its own. This is
# just how quickly we notice it died.
CHECK_INTERVAL = 30

# Colab's assign endpoint returns 4xx once the runtime is really gone. Google's
# daemon gives up after 2 in a row; re-spawning it into a dead runtime forever
# would be a retry storm against a wall (and retry storms are exactly what trips
# abuse heuristics), so we stop after this many failed respawns and say so.
MAX_RESPAWNS = 3


@dataclass
class Health:
    running: bool
    pid: Optional[int]
    respawns: int
    last_spawn: Optional[float]
    gave_up: bool = False
    reason: str = ""


class KeepAliveSupervisor:
    """Ensures Google's keep-alive daemon is running for one named session."""

    def __init__(self, name: str,
                 on_event: Optional[Callable[[str], None]] = None,
                 check_interval: int = CHECK_INTERVAL):
        self.name = name
        self.on_event = on_event or (lambda _msg: None)
        self.check_interval = max(check_interval, 10)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.health = Health(running=False, pid=None, respawns=0, last_spawn=None)

    # -- google's session store ---------------------------------------------
    def _state(self):
        """The official CLI's record for this session (endpoint, token, pid)."""
        from colab_cli.state import StateStore

        store = StateStore(os.environ.get(CONFIG_ENV) or None)
        return store, store.get(self.name)

    def _record_pid(self, pid: int) -> None:
        """Write the new daemon pid back into Google's store.

        `colab stop` reads `keep_alive_pid` to kill the daemon. If we spawn a
        replacement without recording it, `colab stop` would leave our daemon
        running against a released runtime -- an orphan that keeps pinging.
        """
        store, state = self._state()
        if state is None:
            return
        state.keep_alive_pid = pid
        store.add(state)

    # -- supervision --------------------------------------------------------
    def is_running(self) -> bool:
        _, state = self._state()
        if state is None:
            return False
        return procutil.pid_alive(state.keep_alive_pid)

    def ensure_running(self) -> bool:
        """Spawn the keep-alive daemon if it is not currently alive."""
        store, state = self._state()
        if state is None:
            self.health.gave_up = True
            self.health.reason = f"colab has no session named '{self.name}'"
            return False

        if procutil.pid_alive(state.keep_alive_pid):
            self.health.running = True
            self.health.pid = state.keep_alive_pid
            return True

        if self.health.respawns >= MAX_RESPAWNS:
            self.health.gave_up = True
            self.health.reason = (
                "the keep-alive daemon keeps exiting immediately, which usually "
                "means Colab has released the runtime (quota, or the max lifetime "
                "cap). Check `colabapi status`."
            )
            return False

        # Route through our shim rather than `python -m colab_cli.cli`: on Windows
        # the latter cannot even be imported, and on POSIX this is equivalent.
        # Global flags must come BEFORE the subcommand (Typer callback options),
        # mirroring Google's own spawn_keep_alive.
        cmd = [procutil.python_exe(), "-m", "colabapi._colab_shim"]
        auth = os.environ.get(AUTH_ENV)
        if auth:
            cmd.append(f"--auth={auth}")
        config = os.environ.get(CONFIG_ENV)
        if config:
            cmd.extend(["--config", config])
        cmd.extend(["keep-alive", state.endpoint, self.name])
        pid = procutil.spawn_detached(cmd)
        self._record_pid(pid)
        self.health.running = True
        self.health.pid = pid
        self.health.respawns += 1
        self.health.last_spawn = time.time()
        self.on_event(f"started Google's keep-alive daemon (pid {pid})")
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ok = self.ensure_running()
                if not ok and self.health.gave_up:
                    self.on_event(f"giving up: {self.health.reason}")
                    return
                # A daemon that has been up for a while is healthy, so forgive
                # earlier respawns -- otherwise a session that hiccups once every
                # few hours would eventually exhaust MAX_RESPAWNS and be
                # abandoned while perfectly alive.
                if (self.health.last_spawn
                        and time.time() - self.health.last_spawn > 600
                        and self.is_running()):
                    self.health.respawns = 0
            except Exception as exc:  # noqa: BLE001 - never let the supervisor die
                self.on_event(f"supervisor check failed: {exc}")
            self._stop.wait(self.check_interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="colabapi-keepalive", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def gave_up(self) -> bool:
        return self.health.gave_up
