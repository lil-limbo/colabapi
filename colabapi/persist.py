"""Keep the user's *work* alive across a disconnect, not just the runtime.

There are two separate survival problems, and they are constantly conflated:

  1. Does the Colab VM stay allocated?  -- handled by `keepalive.py`.
  2. Does the command you were running survive your connection dropping?

Solving (1) does nothing for (2). If your WebSocket dies while a training run is
in the foreground of the remote shell, the VM lives on but the process can go
with the shell -- you reconnect to a fresh prompt and your work is gone.

Google's `/colab/tty` backend does wrap the shell in tmux server-side (their own
source says so, in the comment explaining why Ctrl-D does not close a piped
session). But that tmux session is theirs: it is not named by us, no identifier
for it is exposed in the session state, and whether reconnecting with the same
proxy token reattaches the *same* pane or hands you a fresh one is undocumented.
Google's own issue tracker has an open request ("adopt/connect to an existing
Colab backend session") that exists precisely because this reattach contract is
not currently guaranteed for the terminal path.

So we do not rely on it. On connect, colabapi starts (or reattaches to) a tmux
session under a name *we* choose and can predict:

    tmux new-session -A -s colabapi-<session>

`-A` means attach-if-exists, create-otherwise, which makes the command safe to
issue on every reconnect. Now the reattach contract is one we own and can test:
whatever is running inside that named session keeps running when the socket
drops, and reconnecting puts you back in front of it.

Two traps in that one line, both verified against a real nested tmux:

  * **`$TMUX` must be unset before exec'ing tmux.** The shell we land in runs
    *inside* Google's own tmux, so `$TMUX` is set, and a tmux client refuses to
    start with it set ("sessions should be nested with care"). Because we
    `exec`, a refusing tmux would take the whole shell down with it -- the
    bootstrap would kill the very connection it is meant to protect, on every
    single connect. Hence `unset TMUX;` first.

  * **The nesting guard cannot be an exported environment variable.** An early
    version exported a marker before exec'ing and assumed tmux would propagate
    it into the shell it spawns. It does not when a tmux server already exists
    (tmux only merges the `update-environment` whitelist into a pre-existing
    server's sessions) -- and on the Colab backend a server always already
    exists, because Google's outer tmux is running on it. An unpropagated
    marker means a bootstrap line replayed inside our session would exec
    `new-session -A` on the session it is already in: a recursive self-attach,
    the "infinite mirror". So instead the guard asks tmux directly which
    session we are in (`tmux display-message -p '#S'`) and only bootstraps when
    the answer is not our session. Replays are then a clean no-op.

If tmux is somehow unavailable on the VM, the bootstrap quietly does nothing and
you get the plain shell -- degraded (work will not survive a drop) but working.
"""

from __future__ import annotations

SESSION_PREFIX = "colabapi-"


def tmux_session_name(name: str) -> str:
    # tmux treats '.' and ':' as window/pane separators in target specs, so keep
    # the name to characters that can never be misparsed as a target.
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name)
    return f"{SESSION_PREFIX}{safe}"


def bootstrap_command(name: str) -> str:
    """A single shell line that puts the remote shell inside our tmux session.

    Written as a one-liner because it is typed into the remote pty exactly as a
    user would type it -- there is no side channel to upload a script through.

    `exec` replaces the shell with tmux rather than leaving a parent bash idling
    beneath it, so exiting tmux ends the connection cleanly instead of dropping
    you into a stray shell that looks like a bug.

    See the module docstring for why the guard is a `display-message` query and
    why `unset TMUX` is load-bearing.
    """
    session = tmux_session_name(name)
    # Both remote status bars are switched off. The shell runs nested inside
    # Google's tmux and then ours, and each draws its own status line -- so the
    # user's terminal showed up to three stacked green bars of raw tmux
    # bookkeeping ("[colabapi-0:bash*  ...", "[0] 0:tmux*  ...") under
    # colabapi's own. The first set-option (run while $TMUX still points at
    # Google's session) blanks theirs; the "\; set-option status off" chained
    # onto new-session blanks ours, and -A makes that safe to repeat on every
    # reconnect. Only the LOCAL bar (drawn by shellview) remains, which is the
    # one that carries colabapi's actual message.
    return (
        "if command -v tmux >/dev/null 2>&1 && "
        f'[ "$(tmux display-message -p "#S" 2>/dev/null)" != "{session}" ]; '
        "then tmux set-option status off >/dev/null 2>&1; unset TMUX; "
        f"exec tmux new-session -A -s {session} \\; set-option status off; "
        "fi"
    )


def detach_hint(name: str) -> str:
    return (
        f"Your shell runs inside tmux session '{tmux_session_name(name)}' on the "
        "runtime, so anything you start keeps running if the connection drops."
    )
