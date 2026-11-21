"""Which stream a command writes to (LAY-4250).

stdout is the command's result. With ``--format json`` it has to be exactly that
JSON, so it can be piped to ``jq``: progress, confirmations and "nothing found"
lines then go to stderr. With the table format they stay on stdout.
"""

from __future__ import annotations

import sys
from typing import NoReturn

import click

#: ``evaluate run --wait`` and ``bulk eval --wait``: an evaluation they waited for ended in a status
#: other than ``success`` (``failure``, ``cancelled``, ``timeout``, ...). No other command exits with it.
EXIT_EVALUATION_NOT_SUCCESSFUL = 12


def _json(ctx: click.Context) -> bool:
    return bool(ctx.obj["output_format"] == "json")


def echo_status(ctx: click.Context, message: str) -> None:
    """A human-readable line that is not the command's result."""
    click.echo(message, err=_json(ctx))


def echo_empty(ctx: click.Context, message: str) -> None:
    """No results: ``[]`` with ``--format json`` (and ``message`` on stderr), else ``message``."""
    if _json(ctx):
        click.echo("[]")
    click.echo(message, err=_json(ctx))


def exit_unreadable(what: str) -> NoReturn:
    """The SDK returns ``None`` when it cannot read a response at all (empty, not an
    object, or the wrong shape). That is a failure, never "no results"."""
    click.echo(f"Error: could not read {what} from the API response.", err=True)
    sys.exit(1)
