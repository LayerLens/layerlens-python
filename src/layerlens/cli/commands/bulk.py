from __future__ import annotations

import sys
import json

import click

from .._client import get_client, handle_errors, resolve_model, api_error_text, resolve_benchmark
from .._output import EXIT_EVALUATION_NOT_SUCCESSFUL, echo_status
from .._command import LayerLensGroup
from .._formatter import format_value, format_output
from ..._exceptions import APIStatusError, LayerLensError

EVALUATION_COLUMNS = [
    ("id", "ID"),
    ("status", "Status"),
    ("model_name", "Model"),
    ("benchmark_name", "Benchmark"),
]


def _why(err: LayerLensError) -> str:
    """The API's sentence for a refused request, the error's own for any other failure."""
    return api_error_text(err) if isinstance(err, APIStatusError) else str(err)


def _runnable(job: object) -> bool:
    """A JSONL line is a job when it is an object with a model and a benchmark."""
    return isinstance(job, dict) and bool(job.get("model")) and bool(job.get("benchmark"))


def _skip(i: int, job: object) -> None:
    click.echo(f"  [{i}] SKIP - a job needs a model and a benchmark: {json.dumps(job)}", err=True)


@click.group(cls=LayerLensGroup)
def bulk() -> None:
    """Bulk operations.

    \b
    Examples:
      layerlens bulk eval --file jobs.jsonl
      layerlens bulk eval --judge-id <judge-id> --traces trace_ids.txt
    """


@bulk.command("eval")
@click.option(
    "--file",
    "file_path",
    type=click.Path(exists=True),
    help='JSONL file with evaluation jobs (each line: {"model": ..., "benchmark": ...}).',
)
@click.option("--model", "model_id", default=None, help="Model ID, key, or name (use with --benchmark).")
@click.option(
    "--benchmark",
    "benchmark_id",
    default=None,
    help="Benchmark ID, key, or name (use with --model).",
)
@click.option("--judge-id", default=None, help="Judge ID (use with --traces).")
@click.option(
    "--traces",
    "traces_file",
    type=click.Path(exists=True),
    default=None,
    help="File with trace IDs (one per line).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Preview without executing.")
@click.option(
    "--wait",
    is_flag=True,
    default=False,
    help=(
        "With --file or --model/--benchmark: wait for the evaluations to finish; exit 12 if any ends in "
        "anything but success. --judge-id/--traces does not wait."
    ),
)
@click.pass_context
@handle_errors
def bulk_eval(
    ctx: click.Context,
    file_path: str | None,
    model_id: str | None,
    benchmark_id: str | None,
    judge_id: str | None,
    traces_file: str | None,
    dry_run: bool,
    wait: bool,
) -> None:
    """Run evaluations in bulk: from a JSONL file, or a judge over a file of trace ids.

    Three modes:
    1. JSONL file: each line is {"model": "<id>", "benchmark": "<id>"}
    2. Model + benchmark: run a single evaluation (optionally --wait)
    3. Judge + traces file: evaluate many traces with a judge

    With --wait, modes 1 and 2 wait for their evaluations and exit 12 when one
    ends in any status but success, after printing them as usual; a job that was
    skipped, refused or failed makes it exit 1, which wins over 12. Mode 3 does
    not wait for its trace evaluations, with --wait or not.

    \b
    Examples:
      layerlens bulk eval --file jobs.jsonl
      layerlens bulk eval --file jobs.jsonl --dry-run
      layerlens bulk eval --model gpt-4 --benchmark mmlu --wait
      layerlens bulk eval --judge-id <id> --traces trace_ids.txt
    """
    client = get_client(ctx)

    if file_path:
        with open(file_path) as f:
            jobs = []
            for line in f:
                line = line.strip()
                if line:
                    try:
                        jobs.append(json.loads(line))
                    except json.JSONDecodeError:
                        click.echo(f"Skipping invalid JSON line: {line}", err=True)

        if not jobs:
            click.echo("No valid jobs found in file.", err=True)
            sys.exit(1)

        if dry_run:
            # A line the run would skip is reported as it does, and fails the dry run as it fails the run.
            runnable = sum(1 for job in jobs if _runnable(job))
            echo_status(ctx, f"[dry-run] Would create {runnable} evaluation(s):")
            for i, job in enumerate(jobs, 1):
                if _runnable(job):
                    echo_status(ctx, f"  model={job['model']} benchmark={job['benchmark']}")
                else:
                    _skip(i, job)
            if runnable < len(jobs):
                click.echo(
                    f"{len(jobs) - runnable} of {len(jobs)} evaluation(s) would be skipped; see above.", err=True
                )
                sys.exit(1)
            return

        echo_status(ctx, f"Creating {len(jobs)} evaluation(s)...")
        evaluations = []
        for i, job in enumerate(jobs, 1):
            if not _runnable(job):
                _skip(i, job)
                continue
            # A job the API refuses is reported and counted, and the next one still runs.
            try:
                m = resolve_model(client, job["model"])
                b = resolve_benchmark(client, job["benchmark"])
                if m is None or b is None:
                    click.echo(
                        f"  [{i}] SKIP - model={job.get('model')} or benchmark={job.get('benchmark')} not found",
                        err=True,
                    )
                    continue

                ev = client.evaluations.create(model=m, benchmark=b)
            except LayerLensError as err:
                click.echo(f"  [{i}] FAIL - {_why(err)}", err=True)
                continue
            if ev:
                echo_status(ctx, f"  [{i}] Created: {ev.id}")
                evaluations.append(ev)
            else:
                click.echo(f"  [{i}] FAIL", err=True)

        echo_status(ctx, f"\n{len(evaluations)} evaluation(s) created.")

        if wait and evaluations:
            echo_status(ctx, "Waiting for completion...")
            finished = []
            for ev in evaluations:
                try:
                    result = client.evaluations.wait_for_completion(ev)
                except LayerLensError as err:
                    click.echo(f"  {ev.id}: {_why(err)}", err=True)
                    continue
                if result:
                    echo_status(ctx, f"  {result.id}: {format_value(result.status)}")
                    finished.append(result)
                else:
                    click.echo(f"  {ev.id}: disappeared while waiting", err=True)
            evaluations = finished

        if ctx.obj["output_format"] == "json":
            click.echo(format_output(evaluations, "json"))

        # Exit 1 on a partial failure too, after reporting what did succeed.
        failed = len(jobs) - len(evaluations)
        if failed:
            click.echo(f"{failed} of {len(jobs)} evaluation(s) failed; see above.", err=True)
            sys.exit(1)
        # Every job ran; one that ended in anything but success fails a CI step that waited (M2).
        if wait and not all(ev.is_success for ev in evaluations):
            sys.exit(EXIT_EVALUATION_NOT_SUCCESSFUL)

    elif model_id and benchmark_id:
        model = resolve_model(client, model_id)
        if model is None:
            click.echo(f"Model not found: {model_id}", err=True)
            sys.exit(1)
        benchmark = resolve_benchmark(client, benchmark_id)
        if benchmark is None:
            click.echo(f"Benchmark not found: {benchmark_id}", err=True)
            sys.exit(1)

        if traces_file:
            click.echo(
                "Error: --traces requires --judge-id, not --model/--benchmark.",
                err=True,
            )
            sys.exit(1)

        else:
            if dry_run:
                echo_status(ctx, f"[dry-run] Would create evaluation: {model.name} x {benchmark.name}")
                return

            echo_status(ctx, f"Creating evaluation: {model.name} x {benchmark.name}")
            ev = client.evaluations.create(model=model, benchmark=benchmark)
            if ev is None:
                click.echo("Failed to create evaluation.", err=True)
                sys.exit(1)

            echo_status(ctx, f"Evaluation created: {ev.id}")
            if wait:
                echo_status(ctx, "Waiting for completion...")
                ev = client.evaluations.wait_for_completion(ev)
                if ev is None:
                    click.echo("Evaluation disappeared while waiting.", err=True)
                    sys.exit(1)
                echo_status(ctx, f"Evaluation finished: {format_value(ev.status)}")

            output = format_output(ev, ctx.obj["output_format"])
            click.echo(output)
            if wait and not ev.is_success:
                sys.exit(EXIT_EVALUATION_NOT_SUCCESSFUL)
    elif judge_id and traces_file:
        # Mode 3: judge + traces file
        with open(traces_file) as f:
            trace_ids = [line.strip() for line in f if line.strip()]

        if not trace_ids:
            click.echo("No trace IDs found in file.", err=True)
            sys.exit(1)

        if dry_run:
            echo_status(ctx, f"[dry-run] Would create {len(trace_ids)} trace evaluation(s) with judge {judge_id}:")
            for tid in trace_ids:
                echo_status(ctx, f"  trace={tid}")
            return

        echo_status(ctx, f"Creating {len(trace_ids)} trace evaluation(s) with judge {judge_id}...")
        results = []
        for i, trace_id in enumerate(trace_ids, 1):
            try:
                te = client.trace_evaluations.create(trace_id=trace_id, judge_id=judge_id)
            except LayerLensError as err:
                click.echo(f"  [{i}] FAIL (trace={trace_id}) - {_why(err)}", err=True)
                continue
            if te:
                echo_status(ctx, f"  [{i}] Created: {te.id} (trace={trace_id})")
                results.append(te)
            else:
                click.echo(f"  [{i}] FAIL (trace={trace_id})", err=True)

        echo_status(ctx, f"\n{len(results)} trace evaluation(s) created.")

        if ctx.obj["output_format"] == "json":
            click.echo(format_output(results, "json"))

        failed = len(trace_ids) - len(results)
        if failed:
            click.echo(f"{failed} of {len(trace_ids)} trace evaluation(s) failed; see above.", err=True)
            sys.exit(1)
    else:
        click.echo("Provide --file, --model + --benchmark, or --judge-id + --traces.", err=True)
        sys.exit(1)
