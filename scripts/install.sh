#!/usr/bin/env bash
#
# colabapi installer
# ------------------
# Installs the colabapi CLI and Google's official Colab CLI that it drives.
# Safe to re-run. Does NOT require root; installs into the user's home by default.
#
#   curl -fsSL https://raw.githubusercontent.com/lil-limbo/colabapi/main/scripts/install.sh | bash
#
# What this does — and does NOT do:
#   * Installs the Python package `colabapi`.
#   * Installs `google-colab-cli` (Google's official CLI, used for auth + tunnel).
#   * Never asks for, stores, or transmits any Google credentials. Sign-in happens
#     in your browser through Google's own flow.

set -euo pipefail

BOLD="$(printf '\033[1m')"; RESET="$(printf '\033[0m')"
info()  { printf "%s==>%s %s\n" "$BOLD" "$RESET" "$*"; }
warn()  { printf "%s[!]%s %s\n" "$BOLD" "$RESET" "$*" >&2; }

PKGS="colabapi google-colab-cli"

if command -v pipx >/dev/null 2>&1; then
  info "Installing with pipx: $PKGS"
  for p in $PKGS; do pipx install --force "$p" || warn "pipx failed for $p"; done
elif command -v pip3 >/dev/null 2>&1; then
  info "Installing with pip (--user): $PKGS"
  pip3 install --user --upgrade $PKGS
else
  warn "Neither pipx nor pip3 found. Install Python 3.9+ and pip first."
  exit 1
fi

if ! command -v colabapi >/dev/null 2>&1; then
  warn "colabapi is installed but not on your PATH."
  warn 'Add this to your shell profile:  export PATH="$HOME/.local/bin:$PATH"'
fi

if ! command -v colab >/dev/null 2>&1; then
  warn "The official 'colab' CLI is not on your PATH yet."
  warn "See https://github.com/googlecolab/google-colab-cli if 'colabapi doctor' reports it missing."
fi

info "Done. Next: ${BOLD}colabapi login${RESET}  then  ${BOLD}colabapi run${RESET}"
info "Verify your setup any time with: ${BOLD}colabapi doctor${RESET}"
