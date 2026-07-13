# colabapi: a terminal for a persistent Google Colab runtime

**Run Google Colab from your own terminal on Linux, macOS, *and Windows*. Keep the runtime alive after you close the browser, and reach its shell from any VPS or laptop.** `colabapi` is a small, open source command line tool (with an optional desktop window) that turns a Google Colab GPU/TPU session into something you can drive headlessly. Perfect for demos, MVPs, and long running jobs that must survive after the Colab web tab is gone.

> **In one line:** `colabapi` gives you a persistent Colab terminal on your own machine, using Google's official, ban safe sign in and tunnel, and it never sees your Google password.

**Two things you only get here:**

* 🪟 **It works on Windows.** Google's official Colab CLI is [Linux and macOS only](#windows). It fails on Windows before it can even parse a command. colabapi ships the compatibility layer that makes it run in PowerShell and CMD, and it registers as a real Windows app with its own icon and Start menu entry.
* 🔌 **It survives a dropped connection.** Google's terminal sets no WebSocket keepalive and has no reconnect, so one blip ends your session. colabapi pings, reconnects with backoff, and keeps your running job alive on the VM.

<!-- Keywords: google colab terminal, colab cli, colab ssh, colab windows, colab powershell, persistent colab, keep colab alive, headless colab, run colab from terminal, colab gpu terminal, colab from vps, colab session keep alive, colab gui -->

---

## Why colabapi?

Google Colab is fantastic free (and paid) GPU/TPU compute, but it only lives inside a browser tab. Close the tab or lose your connection and the session can go with it. That makes it awkward to:

* **demo an MVP** that needs a GPU without renting a server,
* **reach the runtime from a VPS** or a headless box,
* **register it as a background service** that stays up, or
* **watch CPU / GPU / RAM** from a normal terminal.

`colabapi` solves this by wrapping **Google's official [`google-colab-cli`](https://github.com/googlecolab/google-colab-cli)** with a friendly single command, a graphical window, a systemd service, a runtime picker, a live resource monitor, and a session time display. You sign in through Google's own browser flow; `colabapi` connects over Google's sanctioned tunnel.

## Features

* 🖥 **A desktop app, not just a CLI.** `colabapi ui` opens a clean graphical window (white, minimal, works on Linux and Windows) with one click access to Login, Run, Shell, Monitor, Sessions, Status, Doctor, and Logout. On Windows it opens straight from the Start menu and the app list.
* 🪟 **Works on Windows** (PowerShell + CMD), which Google's own CLI does not. Registers as real installed software with its own logo.
* 🔌 **Reconnects instead of dying.** WebSocket keepalive pings, exponential backoff, and your work keeps running on the VM across the drop.
* 🔐 **Browser sign in, no password handling.** Authentication happens in Google's own login flow (including 2FA / device checks). `colabapi` never asks for, stores, or transmits your Google credentials. `colabapi logout` signs you out again in one command.
* 💻 **Real terminal into the runtime.** `colabapi shell` drops you into a live shell on the Colab VM. `colabapi repl` gives you a Python REPL.
* 🎛 **Runtime picker.** List CPU / T4 / L4 / G4 / A100 / H100 / TPU options; paid tier runtimes are clearly flagged as unavailable on a free account.
* 📈 **Live CPU / GPU / RAM monitor.** `colabapi monitor` streams runtime stats to your terminal (psutil + `nvidia-smi`).
* ⏱ **Session time display.** See uptime and an estimate of how long before Colab's max lifetime cap.
* ♻️ **Keepalive that stays up.** Runs Google's own keepalive daemon and restarts it when it dies, which it otherwise does silently.
* 🧩 **Runs as a background service.** systemd on Linux, a Scheduled Task on Windows, so your session survives logout and reboot.
* 🔎 **Inspectable & MIT licensed.** Read every line. Nothing phones home.

## How it works

```
you  >  colabapi (this tool)  >  colab_cli (Google's official CLI)  >  Google's tunnel  >  your Colab runtime (GPU/TPU VM)

colabapi adds: Windows support, auto-reconnect, keepalive supervision, a desktop
               window, runtime picker, monitor, session timer, background service
colabapi never handles your Google password
```

`colabapi` is an **orchestration and reliability layer**. The heavy lifting (OAuth sign in, allocating the runtime, and the encrypted tunnel) is delegated to Google's first party CLI, which is the safe, supported way to do this. We do not reimplement any of it; we make it run where it otherwise cannot, and keep it running when it otherwise would not.

## Install

**One command installs the whole system.** Google's official Colab CLI is pulled in automatically as a dependency, so you never install it separately.

### With pipx (recommended)

```bash
pipx install colabapi
```

<a name="windows"></a>
### Windows (PowerShell or CMD)

> **Google's official Colab CLI does not support Windows at all.** The docs say Linux and macOS only, and on Windows it raises `ImportError: No module named 'termios'` before it can parse a single command. **colabapi fixes that.** It ships a compatibility layer that supplies the POSIX pieces Windows lacks, so Google's CLI runs here unmodified. We patch nothing inside it, so their updates keep working.

**One line, in PowerShell:**

```powershell
irm https://raw.githubusercontent.com/lil-limbo/colabapi/v0.2.1/scripts/install.ps1 | iex
```

That finds Python 3.12+ (and offers to install it with winget if you have none), installs Python's `pipx` if needed, installs colabapi, fixes your `PATH`, and registers it with Windows. No administrator rights required. The URL is pinned to the released tag, so you can [read exactly what will run](https://github.com/lil-limbo/colabapi/blob/v0.2.1/scripts/install.ps1) before you run it.

**Or by hand** (PowerShell or CMD, identical):

```powershell
python -m pip install --user pipx
python -m pipx ensurepath
pipx install colabapi
colabapi register
```

Then **close and reopen your terminal** so `PATH` refreshes, and check it:

```powershell
colabapi doctor
```

**`colabapi register`** makes it a real Windows program rather than a loose `.exe`:

* it appears in **Settings, Installed apps** (and Add/Remove Programs) with the colabapi logo and a working uninstall entry;
* a **Start menu** entry opens the colabapi window (`colabapi ui`), and typing **`colabapi`** works from **Win+R**, without touching `PATH`.

It writes per-user keys under `HKEY_CURRENT_USER` (so no admin prompt), and `colabapi unregister` removes them, the shortcut, and the icon cleanly.

**Requires Python 3.12+** ([`winget install Python.Python.3.13`](https://learn.microsoft.com/en-us/windows/package-manager/winget/)). Works in Windows Terminal, PowerShell 5.1 and 7, and classic `cmd.exe`; ANSI colours are switched on automatically even on legacy consoles.

### As root (VPS, container, `sudo -i`)

Install system-wide so the command lands in `/usr/local/bin`, which is already on
root's `PATH`:

```bash
pipx install --global colabapi
```

> **Why:** a plain `pipx install` as root puts the script in `/root/.local/bin`, which
> most distros do **not** add to root's `PATH`. The install succeeds but you get
> `colabapi: command not found`. `--global` avoids that. (The one-line install script
> below detects root and does this for you.)

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

No git? Download the ZIP from GitHub ("Code", then "Download ZIP"), unpack it, and run `pip install -e .` inside. With git:

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

**Requirement:** Python 3.12+ (Google's `google-colab-cli` requires it). That's it. Everything else installs with the package.

Verify everything is wired up:

```bash
colabapi doctor
```

## Quickstart

```bash
# 1. Sign in (opens Google's own login in your browser, no password asked)
colabapi login

# 2. Pick a runtime, then name the session when asked
colabapi run                # interactive picker + name prompt
colabapi run --runtime t4   # or go straight to a T4 GPU

# 3. Use it (omit the name to pick from an arrow-key list)
colabapi shell      # terminal on the session, live monitor on top
colabapi monitor    # live CPU / GPU / RAM
colabapi sessions   # list your sessions
colabapi status     # reachability + estimated time remaining
colabapi stop       # stop a session (or: colabapi stop <name>)

# 4. Keep it alive after you log out of your server
colabapi service install
systemctl --user start colabapi
```

Prefer clicking? `colabapi ui` opens the same actions in a window.

Press **Ctrl+C** to leave the monitor; type **`exit`** or press **Ctrl+D** to leave the shell. The Colab runtime keeps running until you stop it or Colab's timers end it.

## The desktop window

```bash
colabapi ui
```

A minimal white window with the same actions as the CLI: sign in, allocate a runtime, open the shell or the live monitor (each in its own terminal window), and check sessions, status, or doctor output right inside the window. The header shows whether you are signed in and how many sessions are active.

* **Windows:** after `colabapi register`, "colabapi" in the Start menu and the app list opens this window directly.
* **Linux:** the window uses Tkinter, which some distros package separately. If `colabapi ui` says Tkinter is missing, install it with `sudo apt install python3-tk` (Debian / Ubuntu / Kali) or `sudo dnf install python3-tkinter` (Fedora).

## Command reference

| Command | What it does |
|---|---|
| `colabapi ui` | Open the colabapi desktop window (all of the below, with buttons). |
| `colabapi login` | Sign in via Google's browser flow (no password handled). |
| `colabapi logout` | Sign out of Google and forget all sessions, for a clean start. |
| `colabapi runtimes` | List runtime types and which need a paid plan. |
| `colabapi run [--runtime KEY]` | Allocate a runtime and name the session (delegates to `colab new -s NAME`). |
| `colabapi sessions` | List the sessions colabapi manages. |
| `colabapi shell [NAME]` | Terminal on a session with a live monitor on top; arrow-key picker if NAME omitted. |
| `colabapi repl [NAME]` | Interactive Python REPL on a session (`colab repl`). |
| `colabapi monitor [NAME]` | Live CPU / GPU / RAM monitor for a session. |
| `colabapi status [NAME]` | Session reachability and estimated time left. |
| `colabapi stop [NAME]` | Stop a session (`colab stop`); arrow-key picker if NAME omitted. |
| `colabapi daemon [NAME]` | Keepalive supervisor: restarts Google's keepalive whenever it dies (used by the service). |
| `colabapi service install\|uninstall\|status` | Manage the background service (systemd on Linux, Scheduled Task on Windows). |
| `colabapi register` / `unregister` | **Windows:** add/remove colabapi from Installed apps + Start menu / Win+R. |
| `colabapi doctor` | Check your environment and the `colab` CLI interface. |
| `colabapi raw -- <args>` | Passthrough to the official `colab` CLI. |

## Running as a background service

The service exists to fix a specific hole: Google's keepalive daemon is a child of *your terminal*. Close the laptop, log out of the VPS, or reboot, and it dies, so your runtime idles out even though nothing was actually wrong with it. Registering colabapi with the OS means the keepalive comes back on its own.

**Linux**, a systemd **user** service (no root required):

```bash
colabapi service install        # writes ~/.config/systemd/user/colabapi.service and enables lingering
systemctl --user start colabapi
systemctl --user status colabapi
```

Lingering (`loginctl enable-linger`) is what lets the service keep running after you disconnect from the VPS, which is exactly what you want for an always-on demo.

**Windows**, a **Scheduled Task** that runs at logon (no administrator rights, unlike a true Windows Service):

```powershell
colabapi service install
schtasks /Run /TN colabapi      # start it now; it also starts at every logon
colabapi service status
```

It appears in Task Scheduler as **colabapi**, and `colabapi service uninstall` removes it.

## Privacy

**We do not capture your login data. We do not collect anything.**

* `colabapi` has **no code path that asks for, reads, stores, or transmits your Google password.** Sign in is delegated entirely to Google's official CLI and happens in your own browser under Google's real login flow.
* `colabapi` operates **no servers**. There is nothing for your data to be sent to. The only network connections are between *your* machine, Google, and (via the official CLI) *your* Colab runtime.
* The only things written to disk are **plain preferences and session bookkeeping** (which runtime you picked and when), under `~/.config/colabapi` and `~/.local/state/colabapi`.
* Signing out is one command: `colabapi logout` removes the Google token that Google's CLI cached and forgets every session.
* The project is **MIT licensed and fully open source.** [Read the code](https://github.com/lil-limbo/colabapi/tree/main/colabapi). If you don't trust a claim here, verify it in the source. That's the point.

## Safety (please read)

`colabapi` deliberately uses **Google's official CLI** instead of the older "SSH into Colab via ngrok/cloudflared" trick, because Colab's own FAQ lists *remote control such as SSH shells* as an activity that can get a runtime or an account terminated. Using the sanctioned path is far safer for your Google account.

**The keepalive is Google's own.** colabapi doesn't invent a scheme to defeat the idle timeout: it runs the keepalive daemon that ships inside Google's CLI, which pings Colab's own tunnel endpoint once a minute. Our reconnect pings are ordinary WebSocket keepalives on our own socket, standard practice for any long-lived connection, and *not* synthetic activity designed to look like a user who isn't there.

Be a good citizen: **don't hold GPU runtimes idle just to reserve them.** Colab's abuse heuristics are real and they do flag paying users. Nothing in colabapi tries to hide what you're doing, and you shouldn't either.

## FAQ

**Does colabapi see or store my Google password?**
No. Sign in is handled by Google's official CLI in your browser. `colabapi` has no password code path at all. `colabapi logout` signs you out again whenever you want.

**How do I keep a Google Colab session alive after closing the browser?**
Allocate a runtime with `colabapi run`, then install the service (`colabapi service install`). Google's keepalive holds off the idle timeout; the systemd service keeps `colabapi` supervising it after you log out.

**Can I get a terminal / shell into Google Colab?**
Yes. `colabapi shell` opens a live PTY on the runtime via Google's `colab console`. `colabapi repl` gives a Python REPL.

**Is there a graphical interface?**
Yes. `colabapi ui` opens a desktop window with buttons for every common action, on Linux and Windows. On Windows, `colabapi register` also puts it in the Start menu and the app list, so you can open it like any other program.

**Can I use a free Colab account?**
Yes. CPU and T4 GPU runtimes are available on the free tier. Paid runtimes (L4, A100, H100, TPU) are shown but flagged; Colab itself refuses them on free accounts.

**How is this different from Google's official `colab` CLI?**
`colabapi` *uses* the official CLI under the hood, and adds the things it doesn't do: **it runs on Windows** (the official one cannot), **it reconnects when the network drops** (the official one has no keepalive and no retry), and it keeps the keepalive daemon alive across logout and reboot. On top of that: one `colabapi` command, a desktop window, a runtime picker with paid tier flags, a live resource monitor, a session timer, and a ready-made background service. If you only need raw commands on Linux, use `colab` directly.

**Does colabapi work on Windows? Google says its CLI doesn't.**
Yes, that's one of the two reasons this project exists. Google's CLI imports `termios`, a POSIX-only module, at startup, so on Windows it dies before running any command at all. colabapi supplies the missing pieces through the Win32 console API, so Google's CLI runs unmodified in PowerShell and CMD. We don't patch their code, so their updates keep working. See [Windows](#windows).

**My session keeps dying. Is that Colab or colabapi?**
Usually neither: it's the *connection*, not the runtime. colabapi's reconnect and keepalive supervision exist exactly for that, and the hard limits that remain (Colab's idle timeout and max lifetime cap) are enforced server-side and shown by `colabapi status`.

**What happens to my running job if my Wi-Fi drops?**
It keeps running. Your shell lives inside a tmux session on the Colab VM, so the job is not attached to your connection; colabapi reconnects and puts you back in front of it. You can also detach on purpose with **Ctrl+]**.

**Does it work on a VPS / headless server?**
Yes, that's the main use case. Sign in once in a browser, then run everything from the server, optionally as a systemd service.

**Is this affiliated with Google?**
No. `colabapi` is an independent, open source wrapper. "Google Colab" is a trademark of Google LLC.

## Contributing

Issues and pull requests welcome. If Google changes the official CLI's flags, the mapping from runtime to flag lives in a single file (`colabapi/runtime.py`) and `colabapi doctor` will flag drift.

## License

MIT. See [LICENSE](LICENSE).
