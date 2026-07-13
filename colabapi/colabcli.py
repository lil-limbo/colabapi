"""Thin wrapper around Google's official Colab CLI (`colab`).

colabapi is an orchestration + persistence + UX layer on top of Google's
first-party `google-colab-cli` (https://github.com/googlecolab/google-colab-cli).
We deliberately delegate authentication, runtime allocation, the interactive
terminal, and the primary keep-alive to Google's own tool, which uses Google's
sanctioned tunnel and OAuth (the ban-safe path). colabapi adds: a single
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

from .platform import IS_WINDOWS

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
        # When set, every session-scoped command targets this named `colab`
        # session via `-s`, so colabapi keeps working even if the account has
        # other sessions running. Set by new_runtime / when a Session is loaded.
        self.session: Optional[str] = None

    # -- discovery -----------------------------------------------------------
    def path(self) -> Optional[str]:
        """Locate the `colab` executable.

        Order matters: because google-colab-cli is a dependency of colabapi, its
        `colab` console script lives in the *same* environment bin dir as the
        running interpreter (this is where pipx/venv/pip --user put it), but pipx
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

    def importable(self) -> bool:
        """Can Google's CLI be imported in this interpreter?

        This is what actually matters on Windows: `colab.exe` is installed and
        looks perfectly healthy, but running it raises ImportError on `termios`
        before it parses a single argument, so the binary's mere existence proves
        nothing. What we need there is the *package*, which we drive in-process
        through `_colab_shim` with the compatibility layer applied.
        """
        import importlib.util

        return importlib.util.find_spec("colab_cli") is not None

    def require(self) -> str:
        if IS_WINDOWS:
            if not self.importable():
                raise ColabCliNotFound(INSTALL_HINT)
            return sys.executable
        p = self.path()
        if not p:
            raise ColabCliNotFound(INSTALL_HINT)
        return p

    def available(self) -> bool:
        if IS_WINDOWS:
            return self.importable()
        return self.path() is not None

    def _command(self, args: Sequence[str]) -> list:
        """Build the argv that runs Google's CLI with `args`.

        POSIX: invoke the `colab` console script by absolute path (a bare `colab`
        is not on PATH under pipx, since pipx does not expose dependency scripts).

        Windows: invoke it as `python -m colabapi._colab_shim`, which installs the
        termios/tty shim and then calls Google's own entry point in-process. Same
        arguments, same exit codes, so every caller below is unchanged.
        """
        if IS_WINDOWS:
            return [sys.executable, "-m", "colabapi._colab_shim", *args]
        return [self.require(), *args]

    # -- low-level invocation ------------------------------------------------
    def _child_env(self) -> dict:
        """Environment for the `colab` subprocess.

        Google frequently returns OAuth scopes in a different order (or a subset)
        than the official CLI requested. When that happens, the `oauthlib` library
        the CLI depends on raises "Scope has changed" and aborts sign-in even
        though authentication actually succeeded. Setting OAUTHLIB_RELAX_TOKEN_SCOPE
        tells oauthlib to accept the returned token, so login completes. We only
        set a default; a user who exported their own value wins.
        """
        env = os.environ.copy()
        env.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        return env

    def _run(self, args: Sequence[str], capture: bool = True,
             timeout: Optional[float] = None,
             input: Optional[str] = None) -> ColabResult:
        proc = subprocess.run(
            self._command(args),
            capture_output=capture,
            text=True,
            timeout=timeout,
            input=input,
            env=self._child_env(),
        )
        return ColabResult(proc.returncode,
                           proc.stdout or "" if capture else "",
                           proc.stderr or "" if capture else "")

    def _exec_tty(self, args: Sequence[str]) -> int:
        """Hand the terminal directly to `colab` for interactive subcommands.

        Inheriting stdio gives the user Google's real PTY behavior for `repl` and
        for the browser sign-in flow without us re-implementing any of it.
        """
        proc = subprocess.run(self._command(args), env=self._child_env())
        return proc.returncode

    def _session_args(self) -> list:
        """`-s <name>` for the colabapi-owned session, or nothing if unset."""
        return ["-s", self.session] if self.session else []

    # -- high-level commands (the single place flags are mapped) --------------
    def version(self) -> str:
        # The official CLI exposes version as a subcommand (`colab version`),
        # not a `--version` flag, and prints e.g. "Version: 0.6.0".
        try:
            out = self._run(["version"], timeout=15).text.strip()
            return out.split(":", 1)[1].strip() if ":" in out else out
        except Exception:
            return "unknown"

    def help_text(self, subcommand: Optional[str] = None) -> str:
        args = ([subcommand] if subcommand else []) + ["--help"]
        try:
            return self._run(args, timeout=15).text
        except Exception as exc:  # noqa: BLE001
            return f"(could not read help: {exc})"

    def login(self) -> int:
        """Trigger Google's browser OAuth.

        The official CLI has no standalone `auth` command; OAuth happens on the
        first call that reaches Colab's backend. We use `colab sessions` (list
        sessions, which needs credentials) as a lightweight trigger that does NOT
        allocate a VM, run with an inherited TTY so Google's interactive login /
        2FA / device checks work normally. If sign-in is still needed, it also
        completes automatically on the first `colab new` (see new_runtime).
        """
        return self._exec_tty(["sessions"])

    def new_runtime(self, colab_flags: Sequence[str], name: Optional[str] = None) -> int:
        """Allocate a runtime via `colab new`, interactively.

        Run with an inherited TTY (not captured) so that if this is the first
        authenticated call, Google's browser OAuth flow runs correctly, and the
        user sees colab's own progress output. Returns the exit code.
        `colab_flags` is the accelerator mapping from runtime.py. When `name` is
        given we create the session as `-s <name>` and remember it, so every
        later command can target exactly this session even if others exist.
        """
        args = ["new"]
        if name:
            args += ["-s", name]
        args += list(colab_flags)
        code = self._exec_tty(args)
        if name and code == 0:
            self.session = name
        return code

    def status(self) -> ColabResult:
        return self._run(["status", *self._session_args()], timeout=30)

    def stop_session(self) -> ColabResult:
        """Stop and release the runtime (`colab stop`), targeting our session."""
        return self._run(["stop", *self._session_args()], timeout=60)

    def console(self) -> int:
        """Interactive shell on the runtime.

        Uses colabapi's own resilient client (see terminal.py) rather than
        `colab console`. Google's console sets no WebSocket keepalive and has no
        reconnect logic, so a suspended laptop or a momentary blip ends the
        session for good; ours pings, reconnects with backoff, and reattaches to
        a tmux session on the VM so running work survives the drop.

        Set COLABAPI_PLAIN_CONSOLE=1 to use Google's implementation instead --
        an escape hatch in case Colab ever changes the protocol under us.
        """
        if self.session and not os.environ.get("COLABAPI_PLAIN_CONSOLE"):
            from .terminal import open_console

            return open_console(self.session)
        return self._exec_tty(["console", *self._session_args()])

    def repl(self) -> int:
        return self._exec_tty(["repl", *self._session_args()])

    def exec_code(self, code: str, timeout: float = 40) -> ColabResult:
        """Run Python code on the runtime and capture its stdout.

        `colab exec` executes Python read from stdin (the documented form is
        `echo '<py>' | colab exec`); it is NOT a shell. The resource monitor and
        keep-alive send small Python snippets through here. With a single active
        session the CLI infers it, so no `--session` is needed.
        """
        return self._run(["exec", *self._session_args()], timeout=timeout, input=code)

    def exec_stream(self, code: str) -> subprocess.Popen:
        """Run long-lived Python on the runtime and stream its stdout back.

        `exec_code` above is one shot: it pays the connection cost (measured at
        ~4s against a live T4) and then throws the connection away. Calling it in
        a loop can therefore never sample faster than that, no matter what
        interval is asked for -- which is why the window's graphs stream instead.
        A program that prints on its own clock delivers each line as it is
        produced (verified: ticks arrive 1.0s apart after the initial connect),
        so one connection gives a genuinely live feed.

        Returns the running process. The caller reads `.stdout` line by line and
        must terminate it when done.
        """
        proc = subprocess.Popen(
            self._command(["exec", *self._session_args()]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # errors belong in the same stream
            text=True,
            bufsize=1,                  # line buffered: a line out is a line in
            env=self._child_env(),
        )
        # The code is the program, delivered on stdin and then EOF'd -- `colab
        # exec` reads until EOF before it runs anything.
        if proc.stdin is not None:
            proc.stdin.write(code)
            proc.stdin.close()
        return proc

    def raw(self, args: Sequence[str]) -> int:
        """Passthrough escape hatch: `colabapi raw -- <args>` -> `colab <args>`."""
        return self._exec_tty(list(args))
