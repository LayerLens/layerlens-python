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
        # On stderr: a script reading stdout (`--format json …`) must not take this for output.
        print(  # noqa: T201
            "CLI dependencies not installed. Install them with:\n\n"
            '  uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[cli]"\n',
            file=sys.stderr,
        )
        sys.exit(1)

    from ._app import cli

    cli()


def legacy_main() -> None:
    """Entry point for the deprecated ``stratix`` command (LAY-4220).

    Identical to ``layerlens``. Its notice goes to stderr, and only when stderr is a terminal,
    so a script sees exactly what 1.11.0 printed, even one that captures ``2>&1`` (R4 F1).
    """
    # Shell completion calls the program on every TAB; a notice there would land in the prompt.
    if "_STRATIX_COMPLETE" not in os.environ:
        from ._notice import notice

        notice("the 'stratix' command is deprecated; use 'layerlens' instead")
    main()


__all__ = ["main", "legacy_main"]
