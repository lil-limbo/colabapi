"""Supervisory keep-alive health check.

The *primary* keep-alive is Google's own: the official `colab` CLI pings Colab's
sanctioned tunnel endpoint on an interval to hold off the idle timeout. This
module is only a belt-and-suspenders supervisor — it runs a trivial command on
the runtime once a minute to confirm the session is still reachable, and records
failures so the systemd service log surfaces a silent death of Google's daemon.

It addresses the IDLE timeout only — it cannot and does not try to defeat Colab's
absolute max-lifetime cap, and it stays gentle (one tiny command per interval) so
it looks like normal use, not abuse.

Honesty note for users: aggressive keep-alive or holding a GPU runtime idle just
to keep it can get a Google account flagged. Keep the interval reasonable.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

RunRemote = Callable[[str], str]

# Minimal, cheap activity — touches a file and reads uptime.
_PING = "date +%s > /tmp/.colabapi_alive && uptime >/dev/null 2>&1 && echo ok"


class KeepAlive:
    def __init__(self, run_remote: RunRemote, interval: int = 60,
                 on_error: Optional[Callable[[Exception], None]] = None):
        self.run_remote = run_remote
        self.interval = max(interval, 30)
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_ok: Optional[float] = None
        self.failures = 0

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                out = self.run_remote(_PING)
                if "ok" in out:
                    self.last_ok = time.time()
                    self.failures = 0
            except Exception as exc:  # noqa: BLE001 - reported via callback
                self.failures += 1
                if self.on_error:
                    self.on_error(exc)
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="colabapi-keepalive", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
