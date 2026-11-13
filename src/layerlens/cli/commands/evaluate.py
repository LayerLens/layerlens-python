from __future__ import annotations

import sys
from typing import Any, List, Tuple, Optional
from datetime import datetime, timezone

import click

from .._client import get_client, handle_errors, resolve_model, resolve_benchmark
from .._output import echo_empty, echo_status, exit_unreadable
from .._command import LayerLensGroup
from .._formatter import to_dict, format_value, format_output
from ..._exceptions import APIStatusError
from .._completions import complete_model, complete_benchmark, complete_evaluation

EVALUATION_COLUMNS = [
    ("id", "ID"),
    ("status", "Status"),
    ("model_name", "Model"),
    ("benchmark_name", "Benchmark"),
    ("accuracy", "Accuracy"),
    ("submitted_at", "Submitted"),
]


def _when(seconds: Any) -> Any:
    """Unix seconds as a UTC date; None (shown ``-``) for 0, which the API sends until a time is set."""
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        return seconds
    if seconds == 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return seconds


def _duration(nanoseconds: Any) -> Any:
    """Nanoseconds, as the API times a prompt, in the unit that reads best; None for 0."""
    if not isinstance(nanoseconds, int) or isinstance(nanoseconds, bool):
        return nanoseconds
    if nanoseconds == 0:
        return None
    seconds = nanoseconds / 1e9
    if seconds < 1:
        return f"{nanoseconds / 1e6:.0f} ms"
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest} s"


def _readable(evaluation: Any) -> Any:
    """An evaluation for a table: dates, and a duration with its unit, where the API sends numbers."""
    row = to_dict(evaluation)
    if not isinstance(row, dict):
        return evaluation
    row = dict(row)
    for key in ("submitted_at", "finished_at"):
        if key in row:
            row[key] = _when(row[key])
    if "average_duration" in row:
        row["average_duration"] = _duration(row["average_duration"])
    return row


def _shown(data: Any, output_format: str, columns: Optional[List[Tuple[str, str]]] = None) -> str:
    """``data`` as ``output_format`` shows it. JSON keeps the API's numbers, which scripts read."""
    if output_format == "json":
        return format_output(data, output_format)
    if isinstance(data, list):
        return format_output([_readable(e) for e in data], output_format, columns)
    return format_output(_readable(data), output_format)


@click.group(cls=LayerLensGroup)
def evaluate() -> None:
    """Manage evaluations.

    \b
    Examples:
      layerlens evaluate list
      layerlens evaluate get <evaluation-id>
      layerlens evaluate run --model gpt-4 --benchmark mmlu --wait
    """


@evaluate.command("list")
@click.option("--page", default=None, type=int, help="Page number.")
@click.option("--page-size", default=None, type=int, help="Results per page.")
@click.option(
    "--status",
    default=None,
    help="Filter by status (pending, in-progress, success, failure).",
)
@click.option(
    "--sort-by",
    default=None,
    type=click.Choice(["submitted_at", "accuracy", "average_duration"]),
    help="Sort field.",
)
@click.option("--order", default=None, type=click.Choice(["asc", "desc"]), help="Sort order.")
@click.pass_context
@handle_errors
def list_evaluations(
    ctx: click.Context,
    page: int | None,
    page_size: int | None,
    status: str | None,
    sort_by: str | None,
    order: str | None,
) -> None:
    """List evaluations with optional filtering and pagination.

    \b
    Examples:
      layerlens evaluate list
      layerlens evaluate list --status success --sort-by accuracy --order desc
      layerlens evaluate list --page-size 5
    """
    from ...models import EvaluationStatus

    client = get_client(ctx)

    eval_status = None
    if status:
        try:
            eval_status = EvaluationStatus(status)
        except ValueError:
            click.echo(
                f"Invalid status: {status}. Valid: {', '.join(s.value for s in EvaluationStatus)}",
                err=True,
            )
            sys.exit(1)

    result = client.evaluations.get_many(
        page=page,
        page_size=page_size,
        status=eval_status,
        sort_by=sort_by,  # type: ignore[arg-type]
        order=order,  # type: ignore[arg-type]
    )
    if result is None:
        exit_unreadable("the evaluation list")
    if not result.evaluations:
        echo_empty(ctx, "No evaluations found.")
        return

    if ctx.obj["verbose"]:
        click.echo(
            f"Showing page {result.pagination.page} of {result.pagination.total_pages} ({result.pagination.total_count} total)",
            err=True,
        )

    click.echo(_shown(result.evaluations, ctx.obj["output_format"], EVALUATION_COLUMNS))


@evaluate.command("get")
@click.argument("id", shell_complete=complete_evaluation)
@click.pass_context
@handle_errors
def get_evaluation(ctx: click.Context, id: str) -> None:
    """Get an evaluation by ID.

    \b
    Examples:
      layerlens evaluate get abc123
      layerlens evaluate get abc123 --format json
    """
    client = get_client(ctx)
    try:
        evaluation = client.evaluations.get_by_id(id)
    except APIStatusError as e:
        # This endpoint is for benchmark evaluations. A common mistake is to
        # pass an ID from `judge test` (a trace evaluation), which lives on a
        # different resource and makes the backend error out. Surface a clean,
        # actionable message instead of a raw 500/404.
        click.echo(f"Error: could not fetch evaluation {id} (HTTP {e.status_code}).", err=True)
        click.echo(
            f"If this ID came from `layerlens judge test`, it is a trace evaluation — "
            f"use `layerlens judge result {id}` instead.",
            err=True,
        )
        sys.exit(1)

    if evaluation is None:
        click.echo(f"Evaluation {id} not found.", err=True)
        sys.exit(1)

    click.echo(_shown(evaluation, ctx.obj["output_format"]))


@evaluate.command("run")
@click.option(
    "--model",
    "model_id",
    required=True,
    shell_complete=complete_model,
    help="Model ID, key, or name.",
)
@click.option(
    "--benchmark",
    "benchmark_id",
    required=True,
    shell_complete=complete_benchmark,
    help="Benchmark ID, key, or name.",
)
@click.option("--wait", is_flag=True, default=False, help="Wait for evaluation to complete.")
@click.pass_context
@handle_errors
def run_evaluation(ctx: click.Context, model_id: str, benchmark_id: str, wait: bool) -> None:
    """Run an evaluation with a model and benchmark.

    The --model and --benchmark options accept an ID, key, or name. An ID can name
    any model or benchmark in the catalog; a key or a name is looked up among the
    project's own.

    \b
    Examples:
      layerlens evaluate run --model gpt-4 --benchmark mmlu
      layerlens evaluate run --model 6a0b692ced57a8fd01d1433a --benchmark 67e20572e014f9fa6c49b996 --wait
      layerlens evaluate run --model "GPT-4" --benchmark "MMLU" --wait --format json
    """
    client = get_client(ctx)

    if ctx.obj["verbose"]:
        click.echo(f"Resolving model: {model_id}", err=True)

    model = resolve_model(client, model_id)
    if model is None:
        click.echo(f"Model not found: {model_id}", err=True)
        sys.exit(1)

    if ctx.obj["verbose"]:
        click.echo(f"Resolved model: {model.name} ({model.id})", err=True)
        click.echo(f"Resolving benchmark: {benchmark_id}", err=True)

    benchmark = resolve_benchmark(client, benchmark_id)
    if benchmark is None:
        click.echo(f"Benchmark not found: {benchmark_id}", err=True)
        sys.exit(1)

    if ctx.obj["verbose"]:
        click.echo(f"Resolved benchmark: {benchmark.name} ({benchmark.id})", err=True)

    echo_status(ctx, f"Creating evaluation: {model.name} x {benchmark.name}")

    evaluation = client.evaluations.create(model=model, benchmark=benchmark)
    if evaluation is None:
        click.echo("Failed to create evaluation.", err=True)
        sys.exit(1)

    echo_status(ctx, f"Evaluation created: {evaluation.id} (status: {format_value(evaluation.status)})")

    if wait:
        echo_status(ctx, "Waiting for completion...")
        evaluation = client.evaluations.wait_for_completion(evaluation)
        if evaluation is None:
            click.echo("Evaluation disappeared while waiting.", err=True)
            sys.exit(1)
        echo_status(ctx, f"Evaluation finished: {format_value(evaluation.status)}")

    click.echo(_shown(evaluation, ctx.obj["output_format"]))
