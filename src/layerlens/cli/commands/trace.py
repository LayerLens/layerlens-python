from __future__ import annotations

import sys
import json
from typing import Any, List, Tuple, Callable

import click

from .._client import get_client, handle_errors
from .._output import echo_empty, echo_status, exit_unreadable
from .._command import LayerLensGroup
from .._formatter import to_dict, format_output
from .._completions import complete_trace

# Agent and Tags come last, so a script that cuts the first columns keeps working.
TRACE_COLUMNS = [
    ("id", "ID"),
    ("created_at", "Created"),
    ("filename", "Filename"),
    ("evaluations_count", "Evaluations"),
    ("agent", "Agent"),
    ("tags", "Tags"),
]


def _listed(ctx: click.Context, traces: List[Any]) -> str:
    """The traces as the chosen format shows them; in a table, tags read as ``a, b``, not JSON."""
    if ctx.obj["output_format"] == "json":
        return format_output(traces, "json")
    rows = []
    for t in traces:
        row = to_dict(t)
        row["tags"] = ", ".join(row.get("tags") or []) or None
        rows.append(row)
    return format_output(rows, "table", TRACE_COLUMNS)


def _tags(ctx: click.Context, param: click.Parameter, value: Tuple[str, ...]) -> List[str]:
    # A blank tag (an unset variable in `--tag "$GIT_SHA"`) would be ignored by the API,
    # which would then list every trace in the project as if it were the run's.
    if any(not tag.strip() for tag in value):
        raise click.BadParameter("a tag cannot be empty", ctx=ctx, param=param)
    return list(value)


def _tag_option(fn: Callable[..., Any]) -> Callable[..., Any]:
    return click.option(
        "--tag",
        "tags",
        multiple=True,
        callback=_tags,
        help="Only traces with this tag. Repeat it for traces with every one.",
    )(fn)


@click.group(cls=LayerLensGroup)
def trace() -> None:
    """Manage traces.

    \b
    Examples:
      layerlens trace list
      layerlens trace get <trace-id>
      layerlens trace search "user login"
      layerlens trace export <trace-id> --output trace.json
      layerlens trace delete <trace-id> --yes
    """


@trace.command("list")
@click.option("--page", default=None, type=int, help="Page number.")
@click.option("--page-size", default=None, type=int, help="Results per page.")
@click.option("--source", default=None, help="Filter by source.")
@click.option("--status", default=None, help="Filter by status.")
@_tag_option
@click.option("--sort-by", default=None, help="Sort field.")
@click.option("--sort-order", default=None, type=click.Choice(["asc", "desc"]), help="Sort order.")
@click.pass_context
@handle_errors
def list_traces(
    ctx: click.Context,
    page: int | None,
    page_size: int | None,
    source: str | None,
    status: str | None,
    tags: List[str],
    sort_by: str | None,
    sort_order: str | None,
) -> None:
    """List traces with optional filtering and pagination.

    \b
    Examples:
      layerlens trace list
      layerlens trace list --page-size 10
      layerlens trace list --source sdk --sort-by created_at --sort-order desc
      layerlens trace list --tag environment --tag support-desk --tag "$GIT_SHA"
    """
    client = get_client(ctx)
    result = client.traces.get_many(
        page=page,
        page_size=page_size,
        source=source,
        status=status,
        tags=tags,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    if result is None:
        exit_unreadable("the trace list")
    if not result.traces:
        echo_empty(ctx, "No traces found.")
        return

    if ctx.obj["verbose"]:
        click.echo(f"Showing {result.count} of {result.total_count} traces", err=True)

    click.echo(_listed(ctx, result.traces))


@trace.command("get")
@click.argument("id", shell_complete=complete_trace)
@click.pass_context
@handle_errors
def get_trace(ctx: click.Context, id: str) -> None:
    """Get a trace by ID.

    \b
    Examples:
      layerlens trace get abc123-def4-5678-ghij-klmnopqrstuv
      layerlens trace get abc123 --format json
    """
    client = get_client(ctx)
    trace = client.traces.get(id)
    if trace is None:
        click.echo(f"Trace {id} not found.", err=True)
        sys.exit(1)

    output = format_output(trace, ctx.obj["output_format"])
    click.echo(output)


@trace.command("search")
@click.argument("query")
@click.option("--page", default=None, type=int, help="Page number.")
@click.option("--page-size", default=None, type=int, help="Results per page.")
@click.option("--source", default=None, help="Filter by source.")
@click.option("--status", default=None, help="Filter by status.")
@_tag_option
@click.option("--sort-by", default=None, help="Sort field.")
@click.option("--sort-order", default=None, type=click.Choice(["asc", "desc"]), help="Sort order.")
@click.pass_context
@handle_errors
def search_traces(
    ctx: click.Context,
    query: str,
    page: int | None,
    page_size: int | None,
    source: str | None,
    status: str | None,
    tags: List[str],
    sort_by: str | None,
    sort_order: str | None,
) -> None:
    """Search traces by query string.

    \b
    Examples:
      layerlens trace search "user login"
      layerlens trace search "error" --source sdk --page-size 5
      layerlens trace search "refund" --tag support-desk
    """
    client = get_client(ctx)
    result = client.traces.get_many(
        search=query,
        page=page,
        page_size=page_size,
        source=source,
        status=status,
        tags=tags,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    if result is None:
        exit_unreadable("the trace search results")
    if not result.traces:
        echo_empty(ctx, "No traces found matching your query.")
        return

    if ctx.obj["verbose"]:
        click.echo(f"Found {result.count} of {result.total_count} traces", err=True)

    click.echo(_listed(ctx, result.traces))


@trace.command("export")
@click.argument("id", shell_complete=complete_trace)
@click.option(
    "--output",
    "-o",
    "output_file",
    default=None,
    type=click.Path(),
    help="Output file path (default: stdout).",
)
@click.pass_context
@handle_errors
def export_trace(ctx: click.Context, id: str, output_file: str | None) -> None:
    """Export a trace as JSON.

    \b
    Examples:
      layerlens trace export abc123
      layerlens trace export abc123 --output trace.json
    """
    client = get_client(ctx)
    trace = client.traces.get(id)
    if trace is None:
        click.echo(f"Trace {id} not found.", err=True)
        sys.exit(1)

    json_str = json.dumps(to_dict(trace), indent=2, default=str)

    if output_file:
        with open(output_file, "w") as f:
            f.write(json_str)
        echo_status(ctx, f"Trace exported to {output_file}")
    else:
        click.echo(json_str)


@trace.command("delete")
@click.argument("id", shell_complete=complete_trace)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
@click.pass_context
@handle_errors
def delete_trace(ctx: click.Context, id: str, yes: bool) -> None:
    """Delete a trace by ID.

    \b
    Examples:
      layerlens trace delete abc123
      layerlens trace delete abc123 --yes
    """
    if not yes:
        # On stderr: a prompt is not output, and stdout may be JSON.
        click.confirm(f"Are you sure you want to delete trace {id}?", abort=True, err=True)

    client = get_client(ctx)
    success = client.traces.delete(id)
    if success:
        echo_status(ctx, f"Trace {id} deleted.")
    else:
        click.echo(f"Failed to delete trace {id}.", err=True)
        sys.exit(1)
