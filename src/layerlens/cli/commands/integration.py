from __future__ import annotations

import sys

import click

from .._client import get_client, handle_errors, api_error_text
from .._output import echo_empty, echo_status, exit_unreadable
from .._command import LayerLensGroup
from .._formatter import format_output
from ..._exceptions import AuthenticationError, InternalServerError, UnprocessableEntityError
from .._completions import complete_integration

INTEGRATION_COLUMNS = [
    ("id", "ID"),
    ("name", "Name"),
    ("type", "Type"),
    ("status", "Status"),
    ("created_at", "Created"),
]


@click.group(cls=LayerLensGroup)
def integration() -> None:
    """Manage integrations.

    \b
    Examples:
      layerlens integration list
      layerlens integration test <integration-id>
    """


@integration.command("list")
@click.option("--page", default=None, type=int, help="Page number.")
@click.option("--page-size", default=None, type=int, help="Results per page.")
@click.pass_context
@handle_errors
def list_integrations(ctx: click.Context, page: int | None, page_size: int | None) -> None:
    """List integrations with optional pagination.

    \b
    Examples:
      layerlens integration list
      layerlens integration list --page-size 10
    """
    client = get_client(ctx)
    result = client.integrations.get_many(page=page, page_size=page_size)
    if result is None:
        exit_unreadable("the integration list")
    if not result.integrations:
        echo_empty(ctx, "No integrations found.")
        return

    if ctx.obj["verbose"]:
        click.echo(f"Showing {result.count} of {result.total_count} integrations", err=True)

    output = format_output(result.integrations, ctx.obj["output_format"], INTEGRATION_COLUMNS)
    click.echo(output)


@integration.command("test")
@click.argument("id", shell_complete=complete_integration)
@click.pass_context
@handle_errors
def test_integration(ctx: click.Context, id: str) -> None:
    """Test an integration by ID.

    \b
    Examples:
      layerlens integration test abc123
      layerlens integration test abc123 --format json
    """
    client = get_client(ctx)
    try:
        result = client.integrations.test(id)
    except UnprocessableEntityError as err:
        # 422: the integration's vendor refused its stored credentials; the API's sentence says why.
        click.echo(f"Error: the stored credentials of integration {id} were refused: {api_error_text(err)}", err=True)
        sys.exit(1)
    except (AuthenticationError, InternalServerError) as err:
        # 401 is LayerLens refusing this caller (get_client has checked the key, so a role: an owner's
        # or admin's key tests integrations), 502 a vendor it could not reach. Neither says the stored
        # credentials are wrong; an API older than the 422 answered a vendor's refusal with 401 too, and
        # its sentence says so. The API's sentence, as it is.
        click.echo(f"Error: integration {id} could not be tested: {api_error_text(err)}", err=True)
        sys.exit(1)
    if result is None:
        click.echo(f"Failed to test integration {id}.", err=True)
        sys.exit(1)

    if result.success:
        echo_status(ctx, f"Integration {id}: OK")
    else:
        echo_status(ctx, f"Integration {id}: FAILED")

    if result.message:
        echo_status(ctx, f"Message: {result.message}")

    if ctx.obj["output_format"] == "json":
        output = format_output(result, "json")
        click.echo(output)

    # A failed check is a failure for a script too, not only for the reader.
    if not result.success:
        sys.exit(1)
