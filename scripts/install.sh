#!/usr/bin/env bash
#
# colabapi installer
# ------------------
# Installs the colabapi CLI. Google's official Colab CLI comes along
# automatically as a dependency, so you only ever install one thing.
# Safe to re-run.
#
#   curl -fsSL https://raw.githubusercontent.com/lil-limbo/colabapi/main/scripts/install.sh | bash
#
# Where it installs:
#   * as root  -> /usr/local/bin (already on PATH for every user, including root,
#                 so `colabapi` works immediately in the same shell)
#   * as a user -> ~/.local/bin (and we run `pipx ensurepath` if it is not on PATH)
#
# What this does, and does NOT do:
#   * Installs the Python package `colabapi` (which pulls in everything it needs).
#   * Never asks for, stores, or transmits any Google credentials. Sign-in happens
#     in your browser through Google's own flow.

set -euo pipefail

BOLD="$(printf '\033[1m')"; RESET="$(printf '\033[0m')"
info()  { printf "%s==>%s %s\n" "$BOLD" "$RESET" "$*"; }
warn()  { printf "%s[!]%s %s\n" "$BOLD" "$RESET" "$*" >&2; }

GLOBAL_BIN_DIR="/usr/local/bin"

is_root() { [ "$(id -u)" -eq 0 ]; }

# pipx grew `--global` in 1.4.0; older builds still honour PIPX_BIN_DIR.
pipx_supports_global() { pipx install --help 2>/dev/null | grep -q -- '--global'; }

install_with_pipx() {
  if is_root; then
    # Root's ~/.local/bin is usually NOT on PATH, which is why a plain
    # `pipx install` as root installs fine but leaves `colabapi: command not
    # found`. Installing into /usr/local/bin avoids that entirely.
    if pipx_supports_global; then
      info "Installing colabapi with pipx (system-wide, into $GLOBAL_BIN_DIR)"
      pipx install --global --force colabapi
    else
      info "Installing colabapi with pipx (system-wide, into $GLOBAL_BIN_DIR)"
      PIPX_BIN_DIR="$GLOBAL_BIN_DIR" \
      PIPX_HOME="/opt/pipx" \
        pipx install --force colabapi
    fi
  else
    info "Installing colabapi with pipx"
    pipx install --force colabapi
    # Puts ~/.local/bin on PATH in the user's shell profile for future shells.
    pipx ensurepath >/dev/null 2>&1 || true
  fi
}

install_with_pip() {
  if is_root; then
    # System-wide install: scripts land in /usr/local/bin, which is on PATH.
    # PEP 668 (Debian/Kali/Ubuntu) guards the system Python, so retry with the
    # documented override if the first attempt is refused.
    info "Installing colabapi with pip (system-wide)"
    pip3 install --upgrade colabapi \
      || pip3 install --upgrade --break-system-packages colabapi
  else
    info "Installing colabapi with pip (--user)"
    pip3 install --user --upgrade colabapi \
      || pip3 install --user --upgrade --break-system-packages colabapi
  fi
}

if command -v pipx >/dev/null 2>&1; then
  install_with_pipx
elif command -v pip3 >/dev/null 2>&1; then
  install_with_pip
else
  warn "Neither pipx nor pip3 found. Install Python 3.9+ and pip first."
  exit 1
fi

# `hash -r` clears bash's cached command lookups, so a freshly installed
# colabapi is visible to the check below without opening a new shell.
hash -r 2>/dev/null || true

if ! command -v colabapi >/dev/null 2>&1; then
  warn "colabapi is installed but not on your PATH."
  if is_root; then
    warn "Expected it in $GLOBAL_BIN_DIR. Add that to PATH, or call it directly:"
    warn "  $GLOBAL_BIN_DIR/colabapi doctor"
  else
    warn 'Open a new shell, or run:  export PATH="$HOME/.local/bin:$PATH"'
  fi
fi

info "Done. Next: ${BOLD}colabapi login${RESET}  then  ${BOLD}colabapi run${RESET}"
info "Verify your setup any time with: ${BOLD}colabapi doctor${RESET}"
