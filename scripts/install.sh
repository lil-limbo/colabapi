#!/usr/bin/env bash
#
# colabapi installer
# ------------------
# Installs the colabapi CLI. Google's official Colab CLI comes along
# automatically as a dependency — you only ever install one thing.
# Safe to re-run. Does NOT require root; installs into the user's home by default.
#
#   curl -fsSL https://raw.githubusercontent.com/lil-limbo/colabapi/main/scripts/install.sh | bash
#
# What this does — and does NOT do:
#   * Installs the Python package `colabapi` (which pulls in everything it needs).
#   * Never asks for, stores, or transmits any Google credentials. Sign-in happens
#     in your browser through Google's own flow.

set -euo pipefail

BOLD="$(printf '\033[1m')"; RESET="$(printf '\033[0m')"
info()  { printf "%s==>%s %s\n" "$BOLD" "$RESET" "$*"; }
warn()  { printf "%s[!]%s %s\n" "$BOLD" "$RESET" "$*" >&2; }

if command -v pipx >/dev/null 2>&1; then
  info "Installing colabapi with pipx"
  pipx install --force colabapi
elif command -v pip3 >/dev/null 2>&1; then
  info "Installing colabapi with pip (--user)"
  pip3 install --user --upgrade colabapi
else
  warn "Neither pipx nor pip3 found. Install Python 3.9+ and pip first."
  exit 1
fi

if ! command -v colabapi >/dev/null 2>&1; then
  warn "colabapi is installed but not on your PATH."
  warn 'Add this to your shell profile:  export PATH="$HOME/.local/bin:$PATH"'
fi

info "Done. Next: ${BOLD}colabapi login${RESET}  then  ${BOLD}colabapi run${RESET}"
info "Verify your setup any time with: ${BOLD}colabapi doctor${RESET}"
