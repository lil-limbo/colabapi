"""Exercise the reconnect state machine without a real network.

We stub out the one method that touches a socket (`_connect_once`) and drive the
loop through the scenarios that matter: a transient drop must reconnect, a clean
exit must NOT reconnect, a dead runtime must stop immediately, and a long series
of successful-then-dropped connections must not hit the give-up ceiling.
"""
import sys
import time
import types

from colabapi import terminal
from colabapi.terminal import HardenedConsole, SessionGone

# Make the test fast: shrink the timers, not the logic.
terminal.BACKOFF_START = 0.01
terminal.BACKOFF_MAX = 0.02
terminal.GIVE_UP_AFTER = 0.5

FAKE = terminal._Endpoint(url="https://x.googleusercontent.com", token="t")
terminal._load_endpoint = lambda name: FAKE

notes = []


def make(script):
    """script: list of outcomes per attempt -- Exception, 'quit', or 'ok-drop'."""
    c = HardenedConsole("s", quiet=True)
    c._note = lambda m: notes.append(m)
    calls = {"n": 0}

    def fake_connect(endpoint):
        i = calls["n"]
        calls["n"] += 1
        step = script[i] if i < len(script) else RuntimeError("net down")
        if step == "quit":
            c._user_quit = True
            return None
        if step == "ok-drop":
            # a socket really opened (terminal.py keeps time on the monotonic
            # clock so suspend/clock-steps cannot corrupt the bookkeeping)
            c._last_connect = terminal._now()
            return RuntimeError("connection reset")
        return step  # an Exception, never connected

    c._connect_once = fake_connect
    return c, calls


# 1. Clean exit: user typed `exit` -> must not reconnect.
notes.clear()
c, calls = make(["quit"])
c._loop()
assert calls["n"] == 1, f"clean exit reconnected ({calls['n']} attempts)"
assert c._exit_code == 0
print("1 OK  clean exit does not reconnect")

# 2. Dead runtime (401) -> stop at once, do not retry into a wall.
notes.clear()
c, calls = make([RuntimeError("HTTP 401 Unauthorized")])
c._loop()
assert calls["n"] == 1, f"retried a dead runtime ({calls['n']} attempts)"
assert c._exit_code == 1
assert any("ended on Colab's side" in n for n in notes), notes
print("2 OK  401 stops immediately and says the session ended")

# 3. Transient drop then success then clean quit -> reconnects.
notes.clear()
c, calls = make([RuntimeError("timeout"), "ok-drop", "quit"])
c._loop()
assert calls["n"] == 3, f"expected 3 attempts, got {calls['n']}"
assert any("reconnecting in" in n for n in notes), notes
assert any("still running" in n for n in notes), notes
print("3 OK  transient drop reconnects and reassures the user")

# 4. Missing session -> SessionGone -> clean stop.
notes.clear()
def gone(name):
    raise SessionGone("colab has no session named 's'")
_saved = terminal._load_endpoint
terminal._load_endpoint = gone
c, calls = make([])
c._loop()
terminal._load_endpoint = _saved
assert calls["n"] == 0
assert c._exit_code == 1
print("4 OK  missing session stops without a single connect attempt")

# 5. The regression we just fixed: repeated *successful* connections that each
#    drop must keep reconnecting forever, not trip the give-up ceiling.
notes.clear()
c, calls = make(["ok-drop"] * 40 + ["quit"])
start = time.time()
c._loop()
assert calls["n"] == 41, f"gave up early after {calls['n']} attempts"
assert not any("Giving up" in n for n in notes), "hit the give-up ceiling despite reconnecting fine"
print(f"5 OK  40 successful reconnects over {time.time()-start:.2f}s never give up")

# 6. A genuine outage -- never connects -- must give up after GIVE_UP_AFTER.
notes.clear()
c, calls = make([RuntimeError("net down")] * 500)
c._loop()
assert any("Giving up" in n for n in notes), notes
assert c._exit_code == 1
print(f"6 OK  a real outage gives up after the window ({calls['n']} attempts)")

print("\nALL PASS")
