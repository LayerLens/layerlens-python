from __future__ import annotations

import os
import sys


def main() -> None:
    # Handle --version before importing click so it works without the [cli] extra.
    # `-v` alone is a shorthand for it; followed by a command, `-v` is --verbose.
    args = sys.argv[1:]
    if args[:1] == ["--version"] or args == ["-v"]:
        from .._version import __version__

        print(f"layerlens {__version__}")  # noqa: T201
        sys.exit(0)

    try:
        import click  # noqa: F401
    except ImportError:
        print(  # noqa: T201
            "CLI dependencies not installed. Install them with:\n\n"
            '  uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[cli]"\n'
        )
        sys.exit(1)

    from ._app import cli

    cli()


def legacy_main() -> None:
    """Entry point for the deprecated ``stratix`` command (LAY-4220).

    Identical to ``layerlens``; the notice goes to stderr so scripts that parse
    stdout (``--format json``, ``ci report >> $GITHUB_STEP_SUMMARY``) are unaffected.
    """
    # Shell completion calls the program on every TAB; a notice there would land in the prompt.
    if "_STRATIX_COMPLETE" not in os.environ:
        print("warning: the 'stratix' command is deprecated; use 'layerlens' instead", file=sys.stderr)  # noqa: T201
    main()


__all__ = ["main", "legacy_main"]
