"""Regression tests for the bugs found in the v0.2.0 review, run against real
sockets and real processes wherever possible.

The big one: websocket-client (verified on 1.9.0) delivers a *graceful server
close* to `on_error` as the raw ABNF close frame, and `on_close` fires with
(None, None) -- the close status code never reaches on_close. The original
implementation keyed "the user typed exit" off `on_close`'s status being 1000,
which therefore never fired; every clean exit looked like a failure and the
client reconnected the user straight back into a shell they had just left.
These tests drive `_connect_once` against a real local WebSocket server so the
library's actual callback order -- not an assumption about it -- is what is
asserted.

Run:  python tests/test_close_semantics.py
"""

import base64
import hashlib
import socket
import struct
import sys
import threading
import time

from colabapi import terminal
from colabapi.terminal import HardenedConsole, _Endpoint, _close_frame_status

failures = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


# --------------------------------------------------------------------------
# A minimal WebSocket server: enough to accept the handshake and then end the
# connection in a chosen way.
# --------------------------------------------------------------------------
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _serve_once(mode, close_code=1000):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        try:
            if mode == "handshake404":
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")
                return
            data = conn.recv(65536).decode("latin1")
            key = next(l.split(":", 1)[1].strip() for l in data.split("\r\n")
                       if l.lower().startswith("sec-websocket-key:"))
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            conn.sendall((
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode())
            time.sleep(0.3)  # let on_open and the size frame happen
            if mode == "close-frame":
                payload = struct.pack("!H", close_code)
                conn.sendall(bytes([0x88, len(payload)]) + payload)
                time.sleep(0.2)
            elif mode == "abrupt":
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                struct.pack("ii", 1, 0))
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return _Endpoint(url=f"http://127.0.0.1:{port}", token="t")


def connect(mode, close_code=1000):
    c = HardenedConsole("s", quiet=True, persist=False)
    err = c._connect_once(_serve_once(mode, close_code))
    return c, err


# 1. Graceful server close 1000 == the user typed `exit`. Must be recognised as
#    a clean quit (this is exactly what the library does NOT report via on_close).
c, err = connect("close-frame", 1000)
check("server close 1000 -> user quit, no error", c._user_quit and err is None,
      f"user_quit={c._user_quit} err={err!r}")

# 2. A non-normal close code (e.g. 1011 internal error) is a connection
#    failure: reconnectable, and NOT a user quit.
c, err = connect("close-frame", 1011)
check("server close 1011 -> failure, not user quit",
      (not c._user_quit) and err is not None and "1011" in str(err),
      f"user_quit={c._user_quit} err={err!r}")

# 3. An abrupt drop (no close frame -- the NAT-reaped-socket case) is a failure.
c, err = connect("abrupt")
check("abrupt close -> failure, not user quit",
      (not c._user_quit) and err is not None, f"user_quit={c._user_quit} err={err!r}")

# 4. Handshake 404 (runtime gone) surfaces an error that _is_fatal recognises.
c, err = connect("handshake404")
check("handshake 404 -> fatal error",
      err is not None and terminal._is_fatal(err), f"err={err!r}")

# 5. The close-frame parser itself.
class _Frame:
    opcode = 8
    def __init__(self, data): self.data = data

check("_close_frame_status parses 1000", _close_frame_status(_Frame(struct.pack("!H", 1000))) == 1000)
check("_close_frame_status: empty close payload -> 1005", _close_frame_status(_Frame(b"")) == 1005)
check("_close_frame_status: non-frame -> None", _close_frame_status(RuntimeError("x")) is None)

# --------------------------------------------------------------------------
# 6. Detach/EOF must actually END the connection: the pump drains what is
#    queued (so a final "exit" is really sent), then closes the socket. Before
#    the fix, Ctrl+] printed its message and then sat in run_forever until the
#    network happened to fail.
# --------------------------------------------------------------------------
class FakeWS:
    def __init__(self):
        self.sent, self.closed = [], False
    def send(self, m): self.sent.append(m)
    def close(self): self.closed = True

terminal.EOF_GRACE_SECS = 0.05  # keep the test fast; the value is cosmetic
c = HardenedConsole("s", quiet=True)
ws = FakeWS()
connected = threading.Event()
connected.set()
c._connected = connected
c._outbox.put({"data": "exit\n"})
c._stop.set()  # what the stdin reader does on EOF / Ctrl+]
c._pump_outbox(ws, connected)
check("pump drains queued exit then closes the socket",
      any("exit" in m for m in ws.sent) and ws.closed,
      f"sent={ws.sent} closed={ws.closed}")

# --------------------------------------------------------------------------
# 7. The tmux bootstrap line: `unset TMUX` (the backend shell lives inside
#    Google's tmux; without it the exec'd tmux refuses to nest and takes the
#    shell down with it), and the guard must query tmux for the current session
#    name (env-var markers do not propagate into sessions created on a
#    pre-existing tmux server).
# --------------------------------------------------------------------------
from colabapi import persist

boot = persist.bootstrap_command("my run!")
check("bootstrap unsets TMUX before exec", "unset TMUX;" in boot, boot)
check("bootstrap guards via tmux display-message", "tmux display-message -p" in boot, boot)
check("bootstrap sanitises the session name", "colabapi-my-run-" in boot, boot)
check("no stale env-marker guard", "COLABAPI_KEEP" not in boot, boot)

# --------------------------------------------------------------------------
# 8. pid_alive must not be fooled by a zombie child. Before the fix, the
#    supervisor's dead keep-alive daemon stayed a zombie in its process table
#    and os.kill(pid, 0) reported it alive forever -- so it was never respawned.
# --------------------------------------------------------------------------
from colabapi import procutil

pid = procutil.spawn_detached([sys.executable, "-c", "pass"])
deadline = time.time() + 10
ok = False
while time.time() < deadline:
    if not procutil.pid_alive(pid):
        ok = True
        break
    time.sleep(0.1)
check("pid_alive reaps and reports an exited detached child", ok, f"pid={pid}")

# --------------------------------------------------------------------------
# 9. Windows special-key translation (pure logic, testable anywhere).
# --------------------------------------------------------------------------
from colabapi.terminal import translate_windows_key

check("win: up arrow -> ESC [ A", translate_windows_key("\xe0", "H") == "\x1b[A")
check("win: delete -> ESC [ 3 ~", translate_windows_key("\xe0", "S") == "\x1b[3~")
check("win: F5 (0x00 prefix) -> ESC [ 1 5 ~", translate_windows_key("\x00", "?") == "\x1b[15~")
check("win: unknown scan code is dropped, not leaked", translate_windows_key("\xe0", "\x99") == "")

# --------------------------------------------------------------------------
# 10. _colab_shim must survive SystemExit carrying a message string.
# --------------------------------------------------------------------------
import types
import colab_cli  # ensure the real package imports first

fake = types.ModuleType("colab_cli.cli")
fake.main = lambda: (_ for _ in ()).throw(SystemExit("boom"))
sys.modules["colab_cli.cli"] = fake
try:
    from colabapi import _colab_shim
    rc = _colab_shim.main(["whatever"])
    check("shim maps SystemExit('msg') to exit code 1", rc == 1, f"rc={rc}")
finally:
    del sys.modules["colab_cli.cli"]

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL PASS")
