"""A resilient terminal into the Colab runtime.

This replaces Google's `colab console` for `colabapi shell`. It speaks the same
protocol to the same endpoint -- a WebSocket at

    wss://{netloc}/colab/tty?colab-runtime-proxy-token={token}

carrying JSON frames of `{"data": "..."}` for keystrokes and output, and
`{"cols": N, "rows": N}` for resizes -- so nothing about how we talk to Google
changes. What changes is how the *client* behaves when the network misbehaves.

Google's implementation (colab_cli/console.py) calls `ws.run_forever()` with no
arguments. In websocket-client that means:

  * `ping_interval=0` -- no WebSocket keepalive pings are ever sent. A TCP
    connection that carries no bytes for a few minutes gets silently reaped by
    NAT tables, corporate proxies and load balancers. Neither side is told. The
    socket is then "half-open": we think we are connected, we are not, and the
    terminal simply hangs until something forces the truth out. This is the
    single most common way a working session appears to "die for no reason".
  * no reconnect -- the first blip, VPN reconnect, laptop suspend or Wi-Fi
    handover ends the session for good, and you get "Connection closed."

So this client:

  * sends WebSocket pings every `PING_INTERVAL`s and fails fast when a pong does
    not come back within `PING_TIMEOUT`s, which converts a silent half-open
    socket into a prompt, detectable disconnect;
  * reconnects automatically with exponential backoff and jitter, re-reading the
    session token each attempt so a refreshed token is picked up;
  * distinguishes a dead *connection* (retry) from a dead *runtime* (stop and
    say so) -- conflating those two is why disconnects get misread as Colab
    killing the session;
  * keeps one long-lived, interruptible stdin reader across reconnects. Google
    spawns a fresh reader thread per connection while the previous one is still
    blocked in `sys.stdin.read(1)`; after a reconnect two threads race for your
    keystrokes and scramble their order.

A subtlety that shaped the close handling: websocket-client (verified against
1.9.0) does NOT deliver the server's close status code to `on_close` -- a
graceful server close arrives as an `on_error` callback carrying the raw ABNF
close *frame* (opcode 8, status packed in the first two payload bytes), and
`on_close` then fires with `(None, None)`. Keying "did the user type exit?" off
`on_close`'s status code therefore never works: every close would look like a
failure and the client would "helpfully" reconnect someone who just left. We
parse the close frame in `on_error` instead.

Why your work survives a reconnect: `persist.py` puts the remote shell inside a
tmux session we name and own; reconnecting reattaches to it, so long-running
commands keep running across a drop and you land back in the shell you left.

None of this touches Colab's own idle or lifetime policy. These are transport
keepalives -- pings on our own socket -- not synthetic activity on the runtime,
and they do not attempt to look like a user who is not there. The hard caps in
`timing.py` still apply and are still reported honestly.
"""

from __future__ import annotations

import codecs
import json
import os
import random
import struct
import sys
import threading
import time
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Callable, Optional
from urllib.parse import urlparse

from .platform import IS_WINDOWS

# WebSocket keepalive. 20s is comfortably under the ~60s idle timeout that most
# NATs and L7 proxies enforce, so the connection never looks idle to them; the
# 10s pong deadline means a half-open socket is detected in well under a minute
# instead of hanging indefinitely.
PING_INTERVAL = 20
PING_TIMEOUT = 10

# Reconnect backoff: quick first retries (most drops are a blip), then back off
# so we are not hammering Google, capped so a long outage still recovers
# promptly once the network returns.
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
BACKOFF_FACTOR = 2.0

# A connection that stayed up at least this long counts as "stable": its drop is
# a fresh incident, so the backoff resets. A connection that dies within this
# window (proxy accepts the socket, runtime kills it a second later) keeps the
# backoff growing -- otherwise connect/insta-drop cycles would retry ~1/s
# forever, which is exactly the retry-storm pattern we refuse to be.
STABLE_CONNECTION_SECS = 30.0

# Give up only after this long without a successful connection. A laptop can be
# suspended, or Wi-Fi gone, for a long time; the runtime outlives that, so we
# should too, rather than quitting after a handful of tries. Measured on the
# monotonic clock, which (on Linux) does not advance during suspend -- wall
# clock would count the hours the lid was closed and give up on resume.
GIVE_UP_AFTER = 15 * 60

# After sending a final "exit" (piped stdin hit EOF) give the remote shell this
# long to flush its goodbye output before we close the socket ourselves. Same
# rationale (and value) as Google's PIPED_EOF_GRACE_SECONDS.
EOF_GRACE_SECS = 0.5

# Local escape hatch. In raw mode Ctrl+C belongs to the *remote* shell, so we
# need our own key to bail out of a wedged connection: Ctrl+] (as in telnet).
ESCAPE_BYTE = 0x1D

# Our clock. Monotonic so suspend/clock-step cannot corrupt the backoff and
# give-up bookkeeping (see GIVE_UP_AFTER). Module-level so tests can reuse it.
_now = time.monotonic

# Windows legacy console key codes -> ANSI sequences the remote shell expects.
#
# This is the FALLBACK path, not the usual one. Measured on Windows Server 2025:
# once ENABLE_VIRTUAL_TERMINAL_INPUT is set (raw mode does that, see _winshim),
# the console translates special keys itself and msvcrt.getwch() already returns
# ESC '[' 'A' for Up -- so nothing here is consulted. The table exists for
# consoles that refuse the VT flag (older Windows builds, some terminal hosts),
# where getwch still reports a '\x00'/'\xe0' prefix plus a scan-code character.
# Without it, arrow keys would reach the remote shell as the raw bytes 0xE0 0x48,
# i.e. garbage.
_WIN_VT_KEYS = {
    "H": "\x1b[A",   # up
    "P": "\x1b[B",   # down
    "M": "\x1b[C",   # right
    "K": "\x1b[D",   # left
    "G": "\x1b[H",   # home
    "O": "\x1b[F",   # end
    "R": "\x1b[2~",  # insert
    "S": "\x1b[3~",  # delete
    "I": "\x1b[5~",  # page up
    "Q": "\x1b[6~",  # page down
    ";": "\x1bOP",   # F1
    "<": "\x1bOQ",   # F2
    "=": "\x1bOR",   # F3
    ">": "\x1bOS",   # F4
    "?": "\x1b[15~",  # F5
    "@": "\x1b[17~",  # F6
    "A": "\x1b[18~",  # F7
    "B": "\x1b[19~",  # F8
    "C": "\x1b[20~",  # F9
    "D": "\x1b[21~",  # F10
    "\x86": "\x1b[23~",  # F11
    "\x87": "\x1b[24~",  # F12
}


def translate_windows_key(prefix: str, code: str) -> str:
    """Map a legacy console special-key pair to its ANSI escape sequence.

    Returns "" for keys with no terminal equivalent (they are dropped rather
    than leaking raw scan codes into the remote shell). Split out as a pure
    function so the mapping is testable off-Windows.
    """
    if prefix not in ("\x00", "\xe0"):
        return ""
    return _WIN_VT_KEYS.get(code, "")


class SessionGone(RuntimeError):
    """The runtime itself is gone -- reconnecting cannot help."""


@dataclass
class _Endpoint:
    url: str
    token: str

    def ws_url(self) -> str:
        parsed = urlparse(self.url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}/colab/tty?colab-runtime-proxy-token={self.token}"


def _load_endpoint(name: str) -> _Endpoint:
    """Read the live url + token for `name` from Google's own session store.

    Re-read on every reconnect rather than cached once: if the official CLI
    refreshes the proxy token while we are disconnected, the stale token would
    give us an endless 401 loop.
    """
    from colab_cli.state import StateStore

    from .keepalive import CONFIG_ENV

    state = StateStore(os.environ.get(CONFIG_ENV) or None).get(name)
    if state is None:
        raise SessionGone(f"colab has no session named '{name}'")
    return _Endpoint(url=state.url, token=state.token)


def _is_fatal(error: object) -> bool:
    """True when the failure means the runtime is gone, not merely unreachable.

    401/403 => the token is no longer accepted; 404 => the session does not
    exist. Retrying either just spins. Everything else (DNS, timeouts, resets,
    5xx from the proxy) is treated as transient and worth a retry. Substring
    matching mirrors Google's own `is_terminal_error`, which does the same.
    """
    text = str(error)
    return any(code in text for code in ("401", "403", "404"))


def _close_frame_status(error: object) -> Optional[int]:
    """The close status code if `error` is a server close frame, else None.

    websocket-client hands the raw ABNF close frame (opcode 8) to `on_error`
    when the server closes gracefully -- see the module docstring. The status
    code is big-endian in the first two payload bytes (RFC 6455 5.5.1); a close
    frame with no payload means "no status" (1005).
    """
    if getattr(error, "opcode", None) != 8:
        return None
    data = getattr(error, "data", b"") or b""
    if len(data) >= 2:
        try:
            return struct.unpack("!H", bytes(data[:2]))[0]
        except (struct.error, TypeError):
            return 1005
    return 1005


class _RawTerminal:
    """Put the local terminal in raw mode and guarantee it gets restored.

    Raw mode is what makes a remote shell feel real: every keystroke goes
    straight to the runtime (including Ctrl+C, which must interrupt the *remote*
    process), and the remote side does the echoing. On Windows the same thing is
    achieved through the console API -- see `_winshim`, which supplies the
    termios/tty modules this imports.
    """

    def __init__(self):
        self.is_tty = sys.stdin.isatty()
        self.fd: Optional[int] = sys.stdin.fileno() if self.is_tty else None
        self._saved = None

    def __enter__(self) -> "_RawTerminal":
        if not self.is_tty:
            return self
        if IS_WINDOWS:
            from . import _winshim

            _winshim.install()
        import termios
        import tty

        self._saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd, termios.TCSANOW)
        return self

    def __exit__(self, *exc) -> None:
        if not self.is_tty or self._saved is None:
            return
        import termios

        # Restore unconditionally, even if we are unwinding from an exception:
        # leaving the user's shell in raw mode makes their terminal unusable.
        termios.tcsetattr(self.fd, termios.TCSANOW, self._saved)


def _size() -> tuple[int, int]:
    try:
        s = os.get_terminal_size()
        return s.columns, s.lines
    except OSError:
        return 80, 24


class HardenedConsole:
    """An auto-reconnecting terminal session on a Colab runtime.

    Two front ends drive the same connection. `run()` is the CLI one: it owns the
    real terminal, reads stdin in raw mode and writes the runtime's bytes to
    stdout. `start()` is the embedded one used by the window (see `gui.py`),
    where there is no local terminal at all -- output is handed to `sink`,
    keystrokes arrive through `send()`, and the size comes from `size_fn` because
    a Tk widget has no os.get_terminal_size(). Everything between those two ends
    -- reconnect, pings, close-frame semantics, the tmux bootstrap, the single
    outbox pump -- is shared, so the window cannot drift from the CLI.
    """

    def __init__(self, name: str, quiet: bool = False, persist: bool = True,
                 sink: Optional[Callable[[str], None]] = None,
                 note: Optional[Callable[[str], None]] = None,
                 size_fn: Optional[Callable[[], tuple]] = None):
        self.name = name
        self.quiet = quiet
        self.persist = persist
        # Embedded when someone else owns the screen: no stdin, no raw mode, no
        # SIGWINCH. Deliberately keyed off `sink` rather than a flag, so the two
        # cannot disagree.
        self._sink = sink
        self._note_cb = note
        self._size_fn = size_fn or _size
        self.embedded = sink is not None
        self._outbox: Queue = Queue()
        self._stop = threading.Event()      # tear the whole session down
        # `_connected` is replaced with a FRESH Event for every connection
        # attempt (see _connect_once). If it were shared, a previous pump
        # thread that had been stuck for seconds inside ws.send() on a dying
        # socket would, on failure, clear the flag out from under the *new*
        # connection -- its pump would exit and every keystroke would silently
        # queue forever. A stale pump can only ever clear its own stale Event.
        self._connected = threading.Event()
        self._ws = None
        self._exit_code = 0
        self._user_quit = False              # remote shell exited cleanly
        self._last_connect: Optional[float] = None  # when a socket last opened
        self._pending_bootstrap: Optional[dict] = None  # queued bootstrap message

    # -- local I/O ----------------------------------------------------------
    def _note(self, message: str) -> None:
        """Print a colabapi status line without corrupting the raw terminal.

        In raw mode the terminal does not translate \\n into a carriage return,
        so a bare newline would leave the cursor mid-row and stair-step the
        output. Every line therefore ends \\r\\n.
        """
        if self._note_cb is not None:
            self._note_cb(message)
            return
        if self.quiet:
            return
        sys.stdout.write(f"\r\n\x1b[36m[colabapi]\x1b[0m {message}\r\n")
        sys.stdout.flush()

    def _read_stdin(self) -> None:
        """One reader for the whole session, outliving every reconnect.

        Reads must be interruptible: a thread parked forever in a blocking read
        cannot notice that the session is shutting down, and (in Google's
        version) a second reader gets spawned on reconnect while the first still
        holds a pending read, so the two then split the user's keystrokes
        between them and reorder the input stream. Polling with a short timeout
        avoids both problems, and keystrokes typed while we are disconnected
        queue up and flush on reconnect instead of being lost.

        Input is read in chunks and decoded incrementally: reading one byte per
        message would split multi-byte UTF-8 characters (turning every non-ASCII
        keystroke into U+FFFD) and split escape sequences (one arrow key = three
        WebSocket frames). The incremental decoder holds partial characters
        across chunk boundaries.
        """
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        is_tty = sys.stdin.isatty()
        while not self._stop.is_set():
            chunk = self._read_chunk()
            if chunk is None:
                continue
            if not chunk:  # EOF
                # Piped stdin: the script is done, so ask the remote shell to
                # exit, then let the pump drain the queue (including this exit)
                # and close the socket -- see _pump_outbox. Setting _stop before
                # the send would race the pump into exiting with the "exit"
                # still queued.
                #
                # A TTY that hits EOF is different: the local terminal died
                # under us. That is a *disconnect*, not a request to end the
                # remote job -- typing "exit" into the tmux session would kill
                # the very work persist.py exists to protect -- so just detach.
                if not is_tty:
                    self._outbox.put({"data": "exit\n"})
                self._stop.set()
                return
            if ESCAPE_BYTE in chunk:
                before = chunk[: chunk.index(ESCAPE_BYTE)]
                if before:
                    self._outbox.put({"data": decoder.decode(before)})
                self._note("detaching (Ctrl+]) — the runtime keeps running.")
                self._stop.set()
                return
            text = decoder.decode(chunk)
            if text:
                self._outbox.put({"data": text})

    def _read_chunk(self) -> Optional[bytes]:
        """Return pending input bytes, b"" on EOF, or None if nothing arrived."""
        if IS_WINDOWS:
            return self._read_chunk_windows()

        import select

        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
        except (OSError, ValueError):
            return b""
        if not ready:
            return None
        try:
            return os.read(sys.stdin.fileno(), 4096)
        except OSError:
            return b""

    def _read_chunk_windows(self) -> Optional[bytes]:
        """Console input via msvcrt, as what a terminal would send.

        getwch (not getch): getch returns bytes in the console's OEM code page,
        which mangles any non-ASCII input; getwch returns proper text we can
        encode as UTF-8.

        On special keys, measured on Windows Server 2025 rather than assumed:
        with ENABLE_VIRTUAL_TERMINAL_INPUT set (which `_winshim` does as part of
        raw mode), the console itself translates Up into the three characters
        ESC '[' 'A', and getwch hands them to us one at a time. So on any console
        where VT input can be enabled we do NOT need to translate anything -- the
        ANSI sequence is already what arrives. `translate_windows_key` remains as
        the fallback for consoles too old to accept the flag, where special keys
        still surface as the legacy '\\x00'/'\\xe0' + scan-code pair; there, the
        prefix branch below converts them.

        We drain the whole pending buffer per call instead of taking one
        character, so an escape sequence crosses the wire as a single frame
        rather than three, and a fast typist or a paste is not spread over dozens
        of round trips.

        msvcrt only speaks to the *console*: with stdin piped (`echo ls |
        colabapi shell`) kbhit() would poll a console the input is not coming
        from and the pipe would never be read. Piped stdin gets a plain blocking
        os.read instead -- not interruptible, but a pipe's lifecycle is
        read-everything-then-EOF, and EOF is what initiates shutdown anyway
        (the thread is a daemon, so a late shutdown from elsewhere does not
        keep the process alive).
        """
        import msvcrt

        if not sys.stdin.isatty():
            try:
                return os.read(sys.stdin.fileno(), 4096)
            except (OSError, ValueError):
                return b""
        if not msvcrt.kbhit():
            time.sleep(0.02)
            return None

        out: list[str] = []
        # Bounded so a held-down key or a huge paste cannot spin here forever
        # while the socket goes unserviced.
        while msvcrt.kbhit() and len(out) < 1024:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                # Legacy console (no VT input): the scan code always follows
                # immediately, so this getwch cannot block in practice.
                out.append(translate_windows_key(ch, msvcrt.getwch()))
            else:
                # NB: Ctrl+Z (0x1A) is deliberately passed through, not treated
                # as EOF: in a raw remote shell it is job control (suspend),
                # exactly as on POSIX.
                out.append(ch)
        return "".join(out).encode("utf-8", errors="replace")

    # -- websocket ----------------------------------------------------------
    # Everything we send goes through the outbox queue, never straight to the
    # socket. Keystrokes, resizes and the tmux bootstrap all originate on
    # different threads, and websocket-client offers no ordering guarantee across
    # concurrent sends -- two threads calling ws.send() can interleave their
    # frames and corrupt the stream. One queue, one sender, no interleaving.
    def _queue_size(self) -> None:
        cols, rows = self._size_fn()
        self._outbox.put({"cols": cols, "rows": rows})

    def _bootstrap_persistence(self, connected: threading.Event) -> None:
        """Drop the remote shell into the tmux session we own (see persist.py).

        Safe to repeat on every reconnect: the command attaches to the existing
        session if there is one, so a drop mid-training-run puts you straight
        back in front of the still-running job.

        Two guards against double-typing the line (harmless server-side thanks
        to persist.py's session-name check, but ugly -- a long command echoed
        twice at the prompt):

          * `connected` is the *spawning* connection's own event, not
            `self._connected`: a Timer armed by a connection that died can fire
            after the replacement is already up, and must notice that ITS
            connection is gone rather than piggyback on the new one (caught by
            tests/test_e2e_console.py).
          * `_pending_bootstrap` covers the queue side: a bootstrap that was
            queued but unsent when the socket died survives in the outbox and
            is delivered on the next connection, so nothing new is queued while
            one is in flight.
        """
        from . import persist

        if not connected.is_set() or self._pending_bootstrap is not None:
            return
        msg = {"data": persist.bootstrap_command(self.name) + "\n"}
        self._pending_bootstrap = msg
        self._outbox.put(msg)

    def _pump_outbox(self, ws, connected: threading.Event) -> None:
        """The only thing that writes to the socket. See the note above.

        Also the thing that actually ENDS the connection when the session is
        shutting down (Ctrl+] detach, or EOF on piped stdin): it drains what is
        already queued -- so a final "exit" really reaches the remote shell --
        then closes the WebSocket, which is what makes run_forever() return.
        Without that close, "detach" would print its message and then sit in
        run_forever until the network happened to fail.
        """
        stopping = False
        while connected.is_set():
            try:
                msg = self._outbox.get(timeout=0.2)
            except Empty:
                if self._stop.is_set():
                    stopping = True
                    break
                continue
            try:
                ws.send(json.dumps(msg))
                if msg is self._pending_bootstrap:
                    self._pending_bootstrap = None
            except Exception:  # noqa: BLE001
                # Socket died mid-send. Put it back so it survives the reconnect.
                self._outbox.put(msg)
                connected.clear()
                return
        if stopping or self._stop.is_set():
            # Piped-EOF case: the "exit" we just sent needs a moment to produce
            # the shell's goodbye output before we hang up (Google's client
            # waits the same 0.5s for the same reason).
            time.sleep(EOF_GRACE_SECS)
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _connect_once(self, endpoint: _Endpoint) -> Optional[Exception]:
        """One connection attempt. Returns the error that ended it, or None."""
        import websocket

        failure: Optional[Exception] = None
        connected = threading.Event()  # this attempt's own liveness flag
        self._connected = connected

        def on_open(ws):
            connected.set()
            self._last_connect = _now()
            threading.Thread(
                target=self._pump_outbox, args=(ws, connected),
                name="colabapi-outbox", daemon=True,
            ).start()
            self._queue_size()
            if self.persist:
                # Typed into the remote shell like a user would type it, so give
                # bash a beat to print its prompt first -- characters sent before
                # the pty is ready are simply dropped by the far end.
                threading.Timer(0.4, self._bootstrap_persistence, args=(connected,)).start()

        def on_message(ws, message):  # noqa: ARG001
            try:
                data = json.loads(message)
            except ValueError:
                return
            if "data" in data:
                if self._sink is not None:
                    # Embedded: the caller owns a VT emulator and wants the raw
                    # escape sequences intact, exactly as a terminal would.
                    self._sink(data["data"])
                    return
                # Raw ANSI from the remote pty: write bytes straight through so
                # nothing reinterprets the escape sequences.
                sys.stdout.buffer.write(data["data"].encode("utf-8"))
                sys.stdout.buffer.flush()

        def on_error(ws, error):  # noqa: ARG001
            nonlocal failure
            # A graceful server close is delivered HERE, as the raw close
            # frame, not to on_close -- see the module docstring. 1000 (and a
            # codeless close) mean the remote shell exited: the user typed
            # `exit`. That is a completed session, not a failure, and we must
            # not "helpfully" reconnect them into a shell they just closed.
            # Any other close code (1001 going-away from a restarting proxy,
            # 1006/1011/...) is a connection problem: reconnect.
            code = _close_frame_status(error)
            if code is not None:
                if code in (1000, 1005):
                    self._user_quit = True
                else:
                    failure = RuntimeError(f"server closed the connection (code {code})")
                return
            failure = error if isinstance(error, Exception) else RuntimeError(str(error))

        def on_close(ws, status, msg):  # noqa: ARG001
            connected.clear()

        # Bound the TCP/TLS/WS handshake (and any blocking send) so a black-hole
        # network fails in seconds, not at the OS's multi-minute TCP timeout --
        # otherwise Ctrl+] during a hung connect could take minutes to land.
        websocket.setdefaulttimeout(15)

        ws = websocket.WebSocketApp(
            url=endpoint.ws_url(),
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._ws = ws
        self._install_resize_handler()

        # The two arguments Google leaves out. Without them a dropped connection
        # is invisible and permanent; with them it is detected in ~30s and healed.
        ws.run_forever(ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT)

        connected.clear()
        return failure

    def _install_resize_handler(self) -> None:
        """Forward terminal resizes to the remote pty.

        On Windows there is no SIGWINCH; `_winshim` patches the signal module so
        this same call is routed to a polling thread instead.
        """
        import signal

        # Embedded: the window resizes, not a terminal. `resize()` is called
        # directly by the widget, and signal.signal() would in any case refuse to
        # run off the main thread.
        if self.embedded:
            return
        if not sys.stdin.isatty():
            return
        if IS_WINDOWS:
            from . import _winshim

            _winshim.install()
        signum = getattr(signal, "SIGWINCH", None)
        if signum is None:
            return

        def handler(_sig, _frame):
            if self._connected.is_set():
                self._queue_size()

        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            # Not on the main thread, or unsupported: resizes just won't
            # propagate. Cosmetic, never fatal.
            pass

    # -- driver -------------------------------------------------------------
    def run(self) -> int:
        with _RawTerminal():
            reader = threading.Thread(target=self._read_stdin, name="colabapi-stdin", daemon=True)
            reader.start()
            try:
                self._loop()
            finally:
                self._stop.set()
                if self._ws is not None:
                    try:
                        self._ws.close()
                    except Exception:  # noqa: BLE001
                        pass
                # The reader polls every <=0.2s, so this join is prompt. Without
                # it, a reader still parked in select() could swallow the first
                # keystroke the user types back in their own shell.
                reader.join(timeout=1.0)
        return self._exit_code

    # -- embedded driver (the window) ---------------------------------------
    # The CLI's run() owns stdin and the terminal; a Tk widget owns neither, so
    # the same session is driven from the outside instead: start it, push
    # keystrokes in, push resizes in, close it. The reconnect loop underneath is
    # the identical one.
    def start(self) -> threading.Thread:
        """Run the session on a background thread. Returns that thread."""
        t = threading.Thread(target=self._loop, name=f"colabapi-console-{self.name}",
                             daemon=True)
        t.start()
        return t

    def send(self, text: str) -> None:
        """Send keystrokes to the runtime. Queued, so typing while disconnected
        survives the reconnect rather than being dropped."""
        if text and not self._stop.is_set():
            self._outbox.put({"data": text})

    def resize(self, cols: int, rows: int) -> None:
        if cols > 0 and rows > 0 and not self._stop.is_set():
            self._outbox.put({"cols": cols, "rows": rows})

    def close(self) -> None:
        """End the session. The runtime keeps running; only we hang up."""
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def finished(self) -> bool:
        return self._stop.is_set() or self._user_quit

    def _loop(self) -> None:
        backoff = BACKOFF_START
        first_failure_at: Optional[float] = None

        while not self._stop.is_set():
            try:
                endpoint = _load_endpoint(self.name)
            except SessionGone as exc:
                self._note(f"{exc}. The runtime is gone; nothing to reconnect to.")
                self._exit_code = 1
                return

            attempt_started = _now()
            error = self._connect_once(endpoint)

            if self._user_quit or self._stop.is_set():
                return

            # Did this attempt actually get a live socket? If so the runtime is
            # reachable, so never trip the give-up window -- otherwise a session
            # that reconnects cleanly every few minutes would still be abandoned
            # once the window elapsed. The *backoff* only resets if the
            # connection also stayed up a while: connect-then-instant-drop
            # cycles keep backing off instead of hammering (see
            # STABLE_CONNECTION_SECS).
            if self._last_connect is not None and self._last_connect >= attempt_started:
                first_failure_at = None
                if _now() - self._last_connect >= STABLE_CONNECTION_SECS:
                    backoff = BACKOFF_START

            if error is not None and _is_fatal(error):
                self._note(
                    f"the runtime rejected us ({error}). The session has ended "
                    "on Colab's side — start a new one with `colabapi run`."
                )
                self._exit_code = 1
                return

            # A recoverable drop. Reconnect: persist.py keeps the shell in tmux,
            # so whatever was running is still running.
            now = _now()
            if first_failure_at is None:
                first_failure_at = now
            elif now - first_failure_at > GIVE_UP_AFTER:
                self._note(
                    f"could not reconnect for {GIVE_UP_AFTER // 60} minutes. Giving up. "
                    "Check `colabapi status` — the runtime may still be alive."
                )
                self._exit_code = 1
                return

            # Jitter so that many clients recovering from the same outage do not
            # retry in lockstep.
            delay = min(backoff, BACKOFF_MAX) * (0.5 + random.random())
            self._note(
                f"connection lost{f' ({error})' if error else ''} — "
                f"reconnecting in {delay:.0f}s. Your shell is still running."
            )
            if self._stop.wait(delay):
                return
            backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)


def open_console(name: str, quiet: bool = False, persist: bool = True) -> int:
    """Open a resilient terminal on session `name`. Returns an exit code."""
    return HardenedConsole(name, quiet=quiet, persist=persist).run()
