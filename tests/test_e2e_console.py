"""End-to-end exercise of the hardened console against a fake /colab/tty.

Drives the REAL `open_console` code path -- reader thread, outbox pump,
websocket callbacks, reconnect loop, tmux bootstrap injection and the EOF ->
exit -> graceful-close teardown -- against a local WebSocket server speaking
the same protocol. The one thing stubbed is `_load_endpoint` (there is no real
Colab session store here).

The scripted session:

  connection 1: server drops the TCP connection abruptly (the NAT-reap case).
                The client must treat it as transient and reconnect.
  connection 2: server echoes; the client must (a) inject exactly one tmux
                bootstrap line, (b) deliver what we type into stdin, and
                (c) on stdin EOF send "exit\\n" and, when the server answers
                with a graceful close 1000, finish with exit code 0 instead of
                reconnecting the user into a shell they just left.

Run:  python tests/test_e2e_console.py
"""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time

from colabapi import terminal

failures = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _handshake(conn):
    data = conn.recv(65536).decode("latin1")
    key = next(l.split(":", 1)[1].strip() for l in data.split("\r\n")
               if l.lower().startswith("sec-websocket-key:"))
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    conn.sendall((
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode())


def _recv_frames(conn, timeout):
    """Yield decoded text payloads of masked client frames until timeout/EOF."""
    conn.settimeout(timeout)
    buf = b""
    while True:
        try:
            chunk = conn.recv(65536)
        except (TimeoutError, OSError):
            return
        if not chunk:
            return
        buf += chunk
        while len(buf) >= 2:
            ln = buf[1] & 0x7F
            off = 2
            if ln == 126:
                if len(buf) < 4:
                    break
                ln = struct.unpack("!H", buf[2:4])[0]
                off = 4
            mask, need = buf[1] & 0x80, 0
            need = off + (4 if mask else 0) + ln
            if len(buf) < need:
                break
            opcode = buf[0] & 0x0F
            if mask:
                key, payload = buf[off:off + 4], buf[off + 4:need]
                payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
            else:
                payload = buf[off:need]
            buf = buf[need:]
            yield opcode, payload


def _text_frame(payload: bytes) -> bytes:
    if len(payload) < 126:
        return bytes([0x81, len(payload)]) + payload
    return bytes([0x81, 126]) + struct.pack("!H", len(payload)) + payload


received = {1: [], 2: []}
conn2_up = threading.Event()

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
PORT = srv.getsockname()[1]
srv.listen(2)


def server():
    # connection 1: accept, then drop abruptly before the bootstrap timer fires.
    conn, _ = srv.accept()
    _handshake(conn)
    time.sleep(0.15)
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    conn.close()

    # connection 2: echo, and answer "exit" with a graceful close 1000.
    conn, _ = srv.accept()
    _handshake(conn)
    conn2_up.set()
    for opcode, payload in _recv_frames(conn, timeout=15):
        if opcode == 8:  # client close
            break
        if opcode != 1:
            continue
        try:
            msg = json.loads(payload)
        except ValueError:
            continue
        if "data" not in msg:
            continue  # resize frames
        received[2].append(msg["data"])
        conn.sendall(_text_frame(json.dumps({"data": msg["data"]}).encode()))
        if "exit" in msg["data"]:
            conn.sendall(bytes([0x88, 2]) + struct.pack("!H", 1000))
            time.sleep(0.2)
            break
    conn.close()
    srv.close()


threading.Thread(target=server, daemon=True).start()

# Fast timers for the test; the logic under test is unchanged.
terminal.BACKOFF_START = 0.05
terminal.BACKOFF_MAX = 0.1
terminal.EOF_GRACE_SECS = 0.1
terminal._load_endpoint = lambda name: terminal._Endpoint(
    url=f"http://127.0.0.1:{PORT}", token="tok")

# Feed stdin through a pipe we control.
r, w = os.pipe()
real_stdin = sys.stdin
sys.stdin = os.fdopen(r, "r", closefd=True)


def typist():
    conn2_up.wait(10)
    time.sleep(0.8)  # let the bootstrap timer (0.4s) fire first
    os.write(w, b"ping\n")
    time.sleep(0.3)
    os.close(w)  # EOF -> the client must send "exit\n" and shut down


threading.Thread(target=typist, daemon=True).start()

start = time.time()
code = terminal.open_console("e2e", quiet=True)
elapsed = time.time() - start
sys.stdin = real_stdin

check("exit code 0 after clean remote exit", code == 0, f"code={code}")
check("finished promptly (no reconnect-after-exit loop)", elapsed < 10, f"{elapsed:.1f}s")
boots = [d for d in received[2] if "tmux new-session" in d]
check("reconnected after the abrupt drop (conn 2 served)", bool(received[2]), str(received[2]))
check("exactly one tmux bootstrap on the surviving connection", len(boots) == 1,
      f"{len(boots)} bootstraps")
check("keystrokes reached the remote shell", any("ping" in d for d in received[2]),
      str(received[2]))
check("EOF sent exit to the remote shell", any("exit" in d for d in received[2]),
      str(received[2]))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL PASS")
