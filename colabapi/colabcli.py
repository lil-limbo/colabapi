"""Thin wrapper around Google's official Colab CLI (`colab`).

colabapi is an orchestration + persistence + UX layer on top of Google's
first-party `google-colab-cli` (https://github.com/googlecolab/google-colab-cli).
We deliberately delegate authentication, runtime allocation, the interactive
terminal, and the primary keep-alive to Google's own tool, which uses Google's
sanctioned tunnel and OAuth — the ban-safe path. colabapi adds: a single
`colabapi` command, a systemd service so sessions survive logout, a runtime
picker that flags paid tiers, a live resource monitor, and session-time display.

Everything that invokes `colab` lives here so that if Google changes the CLI's
flags, there is exactly one file to update. The mapping below is validated
against google-colab-cli as documented mid-2026; `colabapi doctor` checks the
live interface and warns on drift, and every command has a raw passthrough
escape hatch (`colabapi raw -- ...`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Sequence

# Env override lets users point at a differently-named binary or a wrapper.
BINARY_ENV = "COLABAPI_COLAB_BIN"
DEFAULT_BINARY = "colab"

# The official CLI ships as a dependency of colabapi, so this should normally
# never be seen. It can only appear on a broken/partial install.
INSTALL_HINT = (
    "The official Google Colab CLI (`colab`) could not be found.\n"
    "It ships as a dependency of colabapi, so reinstalling should fix this:\n"
    "  pipx install --force colabapi\n"
    "If you installed from source, run:  pip install -e .\n"
    "You can also point colabapi at a specific binary:  export COLABAPI_COLAB_BIN=/path/to/colab"
)


class ColabCliNotFound(RuntimeError):
    pass


@dataclass
class ColabResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return self.stdout if self.stdout.strip() else self.stderr


class ColabCLI:
    def __init__(self, binary: Optional[str] = None):
        self.binary = binary or os.environ.get(BINARY_ENV) or DEFAULT_BINARY

    # -- discovery -----------------------------------------------------------
    def path(self) -> Optional[str]:
        """Locate the `colab` executable.

        Order matters: because google-colab-cli is a dependency of colabapi, its
        `colab` console script lives in the *same* environment bin dir as the
        running interpreter (this is where pipx/venv/pip --user put it) — but pipx
        does NOT expose dependency scripts on PATH. So we look next to
        sys.executable first, which makes `pipx install colabapi` self-contained,
        then fall back to PATH for dev setups or an external install.
        """
        exe_dir = os.path.dirname(sys.executable or "")
        if exe_dir:
            for fname in (self.binary, self.binary + ".exe"):
                cand = os.path.join(exe_dir, fname)
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    return cand
        return shutil.which(self.binary)

    def require(self) -> str:
        p = self.path()
        if not p:
            raise ColabCliNotFound(INSTALL_HINT)
        return p

    def available(self) -> bool:
        return self.path() is not None

    # -- low-level invocation ------------------------------------------------
    def _run(self, args: Sequence[str], capture: bool = True,
             timeout: Optional[float] = None) -> ColabResult:
        # Use the resolved absolute path, not the bare name: when colabapi is
        # installed via pipx, `colab` lives in colabapi's venv but is NOT on PATH.
        exe = self.require()
        proc = subprocess.run(
            [exe, *args],
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
        return ColabResult(proc.returncode,
                           proc.stdout or "" if capture else "",
                           proc.stderr or "" if capture else "")

    def _exec_tty(self, args: Sequence[str]) -> int:
        """Hand the terminal directly to `colab` for interactive subcommands.

        Using subprocess with inherited stdio gives the user Google's real
        PTY/keepalive behavior for `console`/`repl` without us re-implementing
        terminal handling. Invoked via the resolved absolute path (see _run).
        """
        exe = self.require()
        proc = subprocess.run([exe, *args])  # inherits stdin/out/err
        return proc.returncode

    # -- high-level commands (the single place flags are mapped) --------------
    def version(self) -> str:
        try:
            return self._run(["--version"], timeout=15).text.strip()
        except Exception:
            return "unknown"

    def help_text(self, subcommand: Optional[str] = None) -> str:
        args = ([subcommand] if subcommand else []) + ["--help"]
        try:
            return self._run(args, timeout=15).text
        except Exception as exc:  # noqa: BLE001
            return f"(could not read help: {exc})"

    def auth(self) -> int:
        """Browser OAuth via Google's own flow. Never handles a password."""
        return self._exec_tty(["auth"])

    def is_authenticated(self) -> bool:
        # `colab status`/`colab auth --status` shape varies; treat a clean
        # status exit as authenticated, and let real commands surface auth errors.
        res = self._run(["status"], timeout=20)
        if res.ok:
            return True
        low = (res.text or "").lower()
        return not ("not signed in" in low or "unauthenticated" in low or "login" in low)

    def new_runtime(self, colab_flags: Sequence[str]) -> ColabResult:
        """Allocate a runtime. `colab_flags` is the accelerator mapping from runtime.py."""
        return self._run(["new", *colab_flags], timeout=180)

    def status(self) -> ColabResult:
        return self._run(["status"], timeout=30)

    def console(self) -> int:
        """Interactive PTY shell on the runtime (Google's `colab console`)."""
        return self._exec_tty(["console"])

    def repl(self) -> int:
        return self._exec_tty(["repl"])

    def exec(self, command: str, timeout: float = 40) -> ColabResult:
        """Run a shell command on the runtime and capture output.

        `colab exec` is used for non-interactive snippets (the resource monitor,
        keep-alive touch). The `--` separator guards against flag parsing.
        """
        return self._run(["exec", "--", "bash", "-lc", command], timeout=timeout)

    def raw(self, args: Sequence[str]) -> int:
        """Passthrough escape hatch: `colabapi raw -- <args>` -> `colab <args>`."""
        return self._exec_tty(list(args))
