"""Session-timing helpers.

Colab enforces two independent limits:
  * an ABSOLUTE max lifetime (roughly 12h free, longer on paid tiers) that
    nothing can extend, and
  * an IDLE timeout that disconnects a runtime doing nothing. This is the one
    the keep-alive addresses.

colabapi can only *estimate* the absolute end because Google does not publish an
exact per-session deadline. The estimate is based on when the runtime came up
plus the tier's typical cap, and is clearly labeled as an estimate.
"""

from __future__ import annotations

import time
from typing import Optional


def human_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def estimated_end(started_at: Optional[float], max_lifetime_hours: float) -> Optional[float]:
    if not started_at:
        return None
    return started_at + max_lifetime_hours * 3600


def remaining(started_at: Optional[float], max_lifetime_hours: float) -> Optional[float]:
    end = estimated_end(started_at, max_lifetime_hours)
    if end is None:
        return None
    return end - time.time()


def session_line(started_at: Optional[float], max_lifetime_hours: float) -> str:
    if not started_at:
        return "runtime not started"
    up = time.time() - started_at
    rem = remaining(started_at, max_lifetime_hours)
    if rem is None:
        return f"up {human_duration(up)}"
    if rem <= 0:
        return f"up {human_duration(up)}, past estimated limit (may disconnect any moment)"
    return f"up {human_duration(up)} · ~{human_duration(rem)} left (est.)"
