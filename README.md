# colabapi: a terminal for a persistent Google Colab runtime

**Run Google Colab from your own terminal, keep the runtime alive after you close the browser, and reach its shell from any VPS or laptop.** `colabapi` is a small, open source command line tool that turns a Google Colab GPU/TPU session into something you can drive headlessly. Perfect for demos, MVPs, and long running jobs that must survive after the Colab web tab is gone.

> **In one line:** `colabapi` gives you a persistent Colab terminal on your own server, using Google's official, ban safe sign in and tunnel, and it never sees your Google password.

<!-- Keywords: google colab terminal, colab cli, colab ssh, persistent colab, keep colab alive, headless colab, run colab from terminal, colab gpu terminal, colab from vps, colab session keep alive -->

---

## Why colabapi?

Google Colab is fantastic free (and paid) GPU/TPU compute, but it only lives inside a browser tab. Close the tab or lose your connection and the session can go with it. That makes it awkward to:

- **demo an MVP** that needs a GPU without renting a server,
- **reach the runtime from a VPS** or a headless box,
- **register it as a background service** that stays up, or
- **watch CPU / GPU / RAM** from a normal terminal.

`colabapi` solves this by wrapping **Google's official [`google-colab-cli`](https://github.com/googlecolab/google-colab-cli)** with a friendly single command, a systemd service, a runtime picker, a live resource monitor, and a session time display. You sign in through Google's own browser flow; `colabapi` connects over Google's sanctioned tunnel.

## Features

- 🔐 **Browser sign in, no password handling.** Authentication happens in Google's own login flow (including 2FA / device checks). `colabapi` never asks for, stores, or transmits your Google credentials.
- 💻 **Real terminal into the runtime.** `colabapi shell` drops you into a live PTY on the Colab VM. `colabapi repl` gives you a Python REPL.
- 🎛 **Runtime picker.** List CPU / T4 / L4 / G4 / A100 / H100 / TPU options; paid tier runtimes are clearly flagged as unavailable on a free account.
- 📈 **Live CPU / GPU / RAM monitor.** `colabapi monitor` streams runtime stats to your terminal (psutil + `nvidia-smi`).
- ⏱ **Session time display.** See uptime and an estimate of how long before Colab's max lifetime cap.
- ♻️ **Keepalive that resists the idle timeout.** Google's official daemon does the primary keepalive; `colabapi` adds a supervisory health check.
- 🧩 **Runs as a Linux service.** `colabapi service install` registers a systemd user service so your session survives logout.
- 🔎 **Inspectable & MIT licensed.** Read every line. Nothing phones home.

## How it works

```
you  >  colabapi (this tool)  >  colab (Google's official CLI)  >  Google's tunnel  >  your Colab runtime (GPU/TPU VM)

colabapi adds: runtime picker, monitor, session timer, systemd service
colabapi never handles your Google password
```

`colabapi` is an **orchestration and UX layer**. The heavy lifting (OAuth sign in, allocating the runtime, and the encrypted terminal tunnel) is delegated to Google's first party CLI, which is the safe, supported way to do this.

## Install

**One command installs the whole system.** Google's official Colab CLI is pulled in automatically as a dependency, so you never install it separately.

### With pipx (recommended)

```bash
pipx install colabapi
```

### With pip

```bash
pip install --user colabapi
```

> **On Kali / Debian / Ubuntu** you may hit `error: externally-managed-environment` (PEP 668). This is the OS protecting its system Python, not a colabapi problem. Use `pipx` (above), a virtualenv, or override it:
>
> ```bash
> pip install --user colabapi --break-system-packages
> ```

### One line install script

```bash
curl -fsSL https://raw.githubusercontent.com/lil-limbo/colabapi/main/scripts/install.sh | bash
```

### From source

```bash
git clone https://github.com/lil-limbo/colabapi.git
cd colabapi
pip install -e .
```

> **On Kali / Debian / Ubuntu**, `pip install -e .` may fail with `externally-managed-environment` (PEP 668). Pick one:
>
> ```bash
> # Option A: override the guard (quickest)
> pip install -e . --break-system-packages
>
> # Option B: use an isolated virtualenv (cleanest)
> python3 -m venv .venv && source .venv/bin/activate && pip install -e .
> ```

**Requirement:** Python 3.9+. That's it. Everything else installs with the package.

Verify everything is wired up:

```bash
colabapi doctor
```

## Quickstart

```bash
# 1. Sign in (opens Google's own login in your browser, no password asked)
colabapi login

# 2. Pick and allocate a runtime
colabapi run                # interactive picker
colabapi run --runtime t4   # or go straight to a T4 GPU

# 3. Use it
colabapi shell      # interactive terminal on the runtime
colabapi monitor    # live CPU / GPU / RAM
colabapi status     # uptime + estimated time remaining

# 4. Keep it alive after you log out of your server
colabapi service install
systemctl --user start colabapi
```

Press **Ctrl+C** to leave the monitor; type **`exit`** or press **Ctrl+D** to leave the shell. The Colab runtime keeps running until you stop it or Colab's timers end it.

## Command reference

| Command | What it does |
|---|---|
| `colabapi login` | Sign in via Google's browser flow (no password handled). |
| `colabapi runtimes` | List runtime types and which need a paid plan. |
| `colabapi run [--runtime KEY]` | Allocate a runtime (delegates to `colab new`). |
| `colabapi shell` | Interactive terminal on the runtime (`colab console`). |
| `colabapi repl` | Interactive Python REPL on the runtime (`colab repl`). |
| `colabapi monitor` | Live CPU / GPU / RAM monitor. |
| `colabapi status` | Session info, reachability, estimated time left. |
| `colabapi stop` | Stop the Colab runtime (`colab stop`) and clear the local session. |
| `colabapi daemon` | Supervisory keepalive (used by the service). |
| `colabapi service install\|uninstall\|status` | Manage the systemd user service. |
| `colabapi doctor` | Check your environment and the `colab` CLI interface. |
| `colabapi raw -- <args>` | Passthrough to the official `colab` CLI. |

## Running as a Linux service

`colabapi` installs as a **systemd user service** (no root required):

```bash
colabapi service install        # writes ~/.config/systemd/user/colabapi.service and enables lingering
systemctl --user start colabapi
systemctl --user status colabapi
```

Lingering (`loginctl enable-linger`) lets the service keep running after you disconnect from the VPS, which is exactly what you want for an always on demo.

## Privacy

**We do not capture your login data. We do not collect anything.**

- `colabapi` has **no code path that asks for, reads, stores, or transmits your Google password.** Sign in is delegated entirely to Google's official CLI and happens in your own browser under Google's real login flow.
- `colabapi` operates **no servers**. There is nothing for your data to be sent to. The only network connections are between *your* machine, Google, and (via the official CLI) *your* Colab runtime.
- The only things written to disk are **plain preferences and session bookkeeping** (which runtime you picked and when), under `~/.config/colabapi` and `~/.local/state/colabapi`.
- The project is **MIT licensed and fully open source.** [Read the code](https://github.com/lil-limbo/colabapi/tree/main/colabapi). If you don't trust a claim here, verify it in the source. That's the point.

## Safety & Colab's limits (please read)

`colabapi` deliberately uses **Google's official CLI** instead of the older "SSH into Colab via ngrok/cloudflared" trick, because Colab's own FAQ lists *remote control such as SSH shells* as an activity that can get a runtime (or an account) terminated. Using the sanctioned path is far safer for your Google account.

Two limits **nobody** can bypass, and `colabapi` doesn't pretend to:

- **Idle timeout (~90 min):** avoided by the keepalive while a session is active.
- **Absolute max lifetime (~12 h free, up to 24 h paid):** a hard cap. `colabapi` shows an *estimate* of time left, but Google enforces the ceiling.

Be a good citizen: don't hold GPU runtimes idle just to reserve them. Aggressive keepalive can get an account flagged.

## FAQ

**Does colabapi see or store my Google password?**
No. Sign in is handled by Google's official CLI in your browser. `colabapi` has no password code path at all.

**How do I keep a Google Colab session alive after closing the browser?**
Allocate a runtime with `colabapi run`, then install the service (`colabapi service install`). Google's keepalive holds off the idle timeout; the systemd service keeps `colabapi` supervising it after you log out.

**Can I get a terminal / shell into Google Colab?**
Yes. `colabapi shell` opens a live PTY on the runtime via Google's `colab console`. `colabapi repl` gives a Python REPL.

**Can I use a free Colab account?**
Yes. CPU and T4 GPU runtimes are available on the free tier. Paid runtimes (L4, A100, H100, TPU) are shown but flagged; Colab itself refuses them on free accounts.

**How is this different from Google's official `colab` CLI?**
`colabapi` *uses* the official CLI under the hood and adds a single `colabapi` command, a runtime picker with paid tier flags, a live resource monitor, a session timer, and a ready made systemd service. If you only need raw commands, use `colab` directly; if you want the persistent service demo workflow, use `colabapi`.

**Does it work on a VPS / headless server?**
Yes, that's the main use case. Sign in once in a browser, then run everything from the server, optionally as a systemd service.

**Is this affiliated with Google?**
No. `colabapi` is an independent, open source wrapper. "Google Colab" is a trademark of Google LLC.

## Contributing

Issues and pull requests welcome. If Google changes the official CLI's flags, the runtime→flag mapping lives in a single file (`colabapi/runtime.py`) and `colabapi doctor` will flag drift.

## License

MIT. See [LICENSE](LICENSE).
