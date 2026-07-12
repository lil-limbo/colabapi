"""Run Google's official Colab CLI in-process, with the Windows shim applied.

On Linux and macOS, colabapi invokes the `colab` console script directly. On
Windows that script exists but is dead on arrival: importing `colab_cli` raises
ImportError on `termios` before argument parsing even starts, so *every*
subcommand fails, not just the interactive terminal.

This module is the replacement. Invoked as

    python -m colabapi._colab_shim <args...>

it installs the termios/tty shim (see `_winshim`), then hands `<args>` to
colab_cli's own entry point in the same process. From colabapi's point of view
it behaves exactly like the `colab` binary -- same argv, same exit code, same
inherited stdin/stdout -- so `colabcli.py` can swap one for the other without
any other code changing.

Running Google's CLI in-process rather than re-implementing it keeps us on the
sanctioned path: OAuth, runtime allocation and the tunnel are still entirely
Google's code. We only supply the two POSIX modules Windows lacks.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from . import _winshim

    # Must happen before colab_cli is imported: the fake termios/tty modules
    # have to already be in sys.modules when Google's `import termios` runs.
    _winshim.install()

    args = list(sys.argv[1:] if argv is None else argv)

    try:
        from colab_cli.cli import main as colab_main
    except ImportError as exc:
        print(
            f"colabapi: could not import Google's Colab CLI ({exc}).\n"
            "Reinstall with:  pipx install --force colabapi",
            file=sys.stderr,
        )
        return 1

    # colab_cli's entry point reads sys.argv, so present ourselves as `colab`.
    # argv[0] is what its --help output will call itself; using the real name
    # keeps its usage strings honest.
    sys.argv = ["colab", *args]
    try:
        colab_main()
    except SystemExit as exc:  # click/typer exit via SystemExit
        # exc.code may be an int, None, or a message string (SystemExit("boom")
        # semantics: print it, exit 1). int(exc.code) on a string would replace
        # the real error with a ValueError traceback.
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        print(exc.code, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
