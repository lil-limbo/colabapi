"""Make `python -m colabapi` equivalent to the `colabapi` command.

The console script is the normal entry point, but `python -m colabapi` is what
works when the scripts directory is not on PATH (a fresh install before the
shell restarts, a venv driven by absolute path, a subprocess that only knows
the interpreter). Same main(), same behaviour.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    main()
