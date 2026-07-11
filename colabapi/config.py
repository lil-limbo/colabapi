"""Local configuration and session state.

Everything colabapi stores lives under the user's XDG config/state directories.
The only thing ever written to disk is non-sensitive session metadata (chosen
runtime, the `colab` session name, timestamps) plus a couple of preferences. No
Google credentials are ever requested, transmitted, or stored. See README
"Privacy".
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


def _xdg(env: str, default: Path) -> Path:
    value = os.environ.get(env)
    return Path(value) if value else default


HOME = Path.home()
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", HOME / ".config") / "colabapi"
STATE_DIR = _xdg("XDG_STATE_HOME", HOME / ".local" / "state") / "colabapi"
DATA_DIR = _xdg("XDG_DATA_HOME", HOME / ".local" / "share") / "colabapi"

CONFIG_FILE = CONFIG_DIR / "config.json"
SESSION_FILE = STATE_DIR / "session.json"  # legacy single-session file (migrated)
SESSIONS_FILE = STATE_DIR / "sessions.json"  # current multi-session registry


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, STATE_DIR, DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # Keep the tree private (config may hold non-Google tokens like preferences).
    try:
        os.chmod(DATA_DIR, 0o700)
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass


@dataclass
class Config:
    """User preferences. Never contains Google credentials."""

    default_runtime: str = "cpu"
    keepalive_interval: int = 60  # seconds between supervisory keep-alive checks
    monitor_interval: float = 2.0  # seconds between resource refreshes

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text())
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        return cls()

    def save(self) -> None:
        ensure_dirs()
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2))


@dataclass
class Session:
    """Local bookkeeping for the runtime `colab` currently has active.

    The official CLI owns the real connection; colabapi only records which
    runtime we asked for and when, so it can show uptime / estimated time left.
    """

    runtime: str = "cpu"
    started_at: Optional[float] = None  # epoch seconds when `colab new` succeeded
    max_lifetime_hours: float = 12.0  # absolute Colab cap (informational estimate)
    name: Optional[str] = None  # the `colab` session name colabapi created (passed as -s)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls) -> Optional["Session"]:
        if not SESSION_FILE.exists():
            return None
        data = json.loads(SESSION_FILE.read_text())
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        ensure_dirs()
        SESSION_FILE.write_text(json.dumps(asdict(self), indent=2))

    @staticmethod
    def clear() -> None:
        SESSION_FILE.unlink(missing_ok=True)

    @property
    def is_active(self) -> bool:
        return self.started_at is not None


@dataclass
class SessionStore:
    """Registry of every named session colabapi currently manages.

    colabapi can own several `colab` sessions at once (each created via
    `colabapi run` with its own name). This is the single source of truth for the
    session pickers used by shell / stop / monitor.
    """

    sessions: list[Session] = field(default_factory=list)

    @classmethod
    def load(cls) -> "SessionStore":
        if SESSIONS_FILE.exists():
            data = json.loads(SESSIONS_FILE.read_text())
            items = []
            for d in data.get("sessions", []):
                known = {k: v for k, v in d.items() if k in Session.__dataclass_fields__}
                items.append(Session(**known))
            return cls(items)
        # One-time migration from the legacy single-session file.
        legacy = Session.load()
        store = cls([legacy] if (legacy and legacy.is_active) else [])
        if legacy:
            store.save()
            Session.clear()
        return store

    def save(self) -> None:
        ensure_dirs()
        SESSIONS_FILE.write_text(
            json.dumps({"sessions": [asdict(s) for s in self.sessions]}, indent=2)
        )

    def add(self, session: Session) -> None:
        self.sessions = [s for s in self.sessions if s.name != session.name]
        self.sessions.append(session)
        self.save()

    def remove(self, name: str) -> None:
        self.sessions = [s for s in self.sessions if s.name != name]
        self.save()

    def get(self, name: str) -> Optional[Session]:
        return next((s for s in self.sessions if s.name == name), None)

    def active(self) -> list[Session]:
        return [s for s in self.sessions if s.is_active and s.name]
