from __future__ import annotations

from typing import Optional

import click

from ._common import (
    DURATION,
    DEFAULT_TIMEOUT,
    EXIT_NOT_FINISHED,
    fail,
    confirm,
    progress,
    emit_json,
    env_client,
    env_errors,
    lost_start,
    json_output,
    start_not_known,
    interrupted_start,
    idempotency_option,
    sigterm_as_interrupt,
)
from ..._command import LayerLensGroup, LayerLensCommand
from ..._formatter import format_table
from ...._exceptions import LayerLensError
from ....resources.environments._shared import new_idempotency_key


@click.group("tasks", cls=LayerLensGroup)
def tasks() -> None:
    """List, generate and delete an environment's task sets.

    \b
    Examples:
      layerlens env tasks list support-desk
      layerlens env tasks list support-desk --set ci
      layerlens env tasks generate support-desk --set generated --count 20 --budget 2.00 --wait
      layerlens env tasks delete support-desk --set ci --yes
    """


@tasks.command("list", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.option("--set", "set_name", default=None, help="List this set's tasks instead of the sets.")
@env_errors
def list_tasks(ctx: click.Context, environment: str, set_name: Optional[str]) -> None:
    """List the task sets, or with --set one set's tasks."""
    task_sets = env_client(ctx).environments.task_sets
    if set_name is None:
        sets = task_sets.list(environment)
        if json_output(ctx):
            emit_json(sets)
        elif sets:
            click.echo(format_table([s.model_dump() for s in sets], [("name", "Set"), ("size", "Tasks")]))
        else:
            progress("This environment has no task sets.")
        return
    task_set = task_sets.get(environment, set_name)
    if json_output(ctx):
        emit_json(task_set)
        return
    rows = [
        {"name": t.name, "version": t.version, "grader": t.grader.kind, "retired": t.retired, "id": t.id}
        for t in task_set.tasks
    ]
    columns = [("name", "Task"), ("version", "Version"), ("grader", "Grader"), ("retired", "Retired"), ("id", "ID")]
    click.echo(format_table(rows, columns))


@tasks.command("generate", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.option("--set", "set_name", required=True, help="The set to add the generated tasks to.")
@click.option("--count", required=True, type=click.IntRange(1, 100), help="How many tasks, 1-100.")
@click.option("--budget", required=True, type=click.FloatRange(min=0, min_open=True), help="Spend cap in USD.")
@click.option("--prompt", default=None, help="What kind of tasks to write. Defaults to the description.")
@click.option(
    "--target",
    default=None,
    type=click.Choice(["any", "discriminating", "hard"]),
    help="Which tasks to keep. Default discriminating.",
)
@click.option("--model", default=None, help="The model that writes the tasks.")
@click.option("--wait", "wait", is_flag=True, default=False, help="Block until generation finishes.")
@click.option("--timeout", default=DEFAULT_TIMEOUT, type=DURATION, help="With --wait. Default 30m.")
@idempotency_option("job")
@env_errors
def generate(
    ctx: click.Context,
    environment: str,
    set_name: str,
    count: int,
    budget: float,
    prompt: Optional[str],
    target: Optional[str],
    model: Optional[str],
    wait: bool,
    timeout: float,
    idempotency_key: Optional[str],
) -> None:
    """Generate tasks into a set from the environment's own answer key. Prints the job id."""
    envs = env_client(ctx).environments
    environment_id = envs.resolve(environment)
    key = idempotency_key or new_idempotency_key()
    try:
        with sigterm_as_interrupt():
            job = envs.task_sets.generate(
                environment_id, set_name, count=count, budget_usd=budget, prompt=prompt, target=target, model=model,
                idempotency_key=key,
            )  # fmt: skip
    except KeyboardInterrupt:
        fail(interrupted_start("job", key), EXIT_NOT_FINISHED)
    except LayerLensError as err:
        if not start_not_known(err):
            raise
        fail(lost_start(err, "job", key))
    progress(f"Generating up to {count} tasks into {set_name!r} (job {job.id})")
    if wait:
        job = envs.jobs.wait(environment_id, job.id, timeout_seconds=timeout)
        if job.status != "success":
            fail(
                f"generation ended {job.status}: {job.error or job.stop_reason or 'no reason recorded'}",
                EXIT_NOT_FINISHED,
            )
        written = (job.result or {}).get("written")
        progress(f"Generation finished: {written if written is not None else 'an unknown number of'} tasks written")
    if json_output(ctx):
        emit_json(job)
    else:
        click.echo(job.id)


@tasks.command("delete", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.option("--set", "set_name", required=True, help="The set to delete.")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
@env_errors
def delete_tasks(ctx: click.Context, environment: str, set_name: str, yes: bool) -> None:
    """Delete every task of a set."""
    confirm(ctx, yes, f"Delete task set {set_name!r} of {environment}?")
    deleted = env_client(ctx).environments.task_sets.delete(environment, set_name)
    if json_output(ctx):
        emit_json({"set_name": set_name, "deleted": deleted})
    else:
        click.echo(f"Deleted {deleted} tasks of {set_name!r}.")
