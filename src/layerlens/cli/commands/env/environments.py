from __future__ import annotations

from typing import Any, Dict, List, Tuple, Optional

import click

from ._common import (
    DURATION,
    DEFAULT_TIMEOUT,
    confirm,
    counted,
    progress,
    emit_json,
    env_client,
    env_errors,
    json_output,
)
from ..._command import LayerLensCommand
from ..._formatter import format_table, format_single
from ....models.environment import JOB_TERMINAL_STATUSES, Tool, Environment, EnvironmentBuild

_STEP_MARKS = {"done": "✓", "running": "…", "failed": "✗"}


@click.command("world-types", cls=LayerLensCommand)
@env_errors
def world_types(ctx: click.Context) -> None:
    """List the apps an environment can simulate. An agent builds each from the description.

    \b
    Examples:
      layerlens env world-types
      layerlens --format json env world-types
    """
    types = env_client(ctx).environments.world_types()
    if json_output(ctx):
        emit_json([{**t.model_dump(mode="json"), "built_by_agent": t.built_by_agent} for t in types])
        return
    rows = [
        {"type": t.type, "title": _title(t.title, t.built_by_agent), "tools": t.tool_count}
        for t in sorted(types, key=lambda t: t.type)
    ]
    click.echo(format_table(rows, [("type", "Type"), ("title", "Title"), ("tools", "Tools")], 60))


def _title(title: str, built_by_agent: bool) -> str:
    # `env create` builds only what an agent builds; the API refuses any other type.
    return title if built_by_agent else f"{title} (env create cannot build it)"


@click.command("list", cls=LayerLensCommand)
@click.option("--search", default=None, help="Only environments whose name contains this.")
@click.option(
    "--hide-members",
    is_flag=True,
    default=False,
    help="Kept for compatibility: an API key's list already leaves out a composite's apps, so this changes nothing.",
)
@env_errors
def list_environments(ctx: click.Context, search: Optional[str], hide_members: bool) -> None:
    """List the project's environments.

    \b
    Examples:
      layerlens env list
      layerlens env list --search support
    """
    environments = env_client(ctx).environments.list(search=search, hide_members=hide_members)
    if json_output(ctx):
        emit_json(environments)
        return
    if not environments:
        progress("No environments found.")
        return
    click.echo(format_table([_row(e) for e in environments], _ENVIRONMENT_COLUMNS))


_ENVIRONMENT_COLUMNS = [
    ("id", "ID"),
    ("name", "Name"),
    ("type", "Type"),
    ("version", "Version"),
    ("tools", "Tools"),
    ("updated_at", "Updated"),
]


def _row(environment: Environment) -> Dict[str, Any]:
    return {
        "id": environment.id,
        "name": environment.name,
        "type": environment.type,
        "version": environment.environment_version,
        "tools": environment.tool_count,
        "updated_at": environment.updated_at.strftime("%Y-%m-%d %H:%M"),
    }


@click.command("get", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@env_errors
def get(ctx: click.Context, environment: str) -> None:
    """Show one environment: its version, tool count and task sets.

    \b
    Examples:
      layerlens env get support-desk
    """
    envs = env_client(ctx).environments
    found = envs.get(environment)
    task_sets = envs.task_sets.list(found.id)
    if json_output(ctx):
        emit_json({"environment": found, "task_sets": task_sets})
        return
    click.echo(format_single(_row(found)))
    if task_sets:
        click.echo("\nTask sets:")
        click.echo(format_table([s.model_dump() for s in task_sets], [("name", "Set"), ("size", "Tasks")]))
    else:
        click.echo("\nNo task sets.")


@click.command("create", cls=LayerLensCommand)
@click.option("--type", "world_type", required=True, help="A world type from `layerlens env world-types`.")
@click.option(
    "--name",
    required=True,
    help='Unique in the organization. With " — " and the type\'s title after it, it also names the app '
    "inside the environment, and that name must fit in 120 characters too.",
)
@click.option(
    "--description",
    default=None,
    help="The company the environment is about, in plain words. This or --description-file.",
)
@click.option(
    "--description-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Read the description from a file.",
)
@click.option(
    "--if-not-exists",
    is_flag=True,
    default=False,
    help="Reuse an environment of this name created from the same type and description; fail if it differs "
    "or its build failed.",
)
@click.option("--wait", "wait", is_flag=True, default=False, help="Block until the build finishes.")
@click.option("--timeout", default=DEFAULT_TIMEOUT, type=DURATION, help="With --wait. Default 30m.")
@env_errors
def create(
    ctx: click.Context,
    world_type: str,
    name: str,
    description: Optional[str],
    description_file: Optional[str],
    if_not_exists: bool,
    wait: bool,
    timeout: float,
) -> None:
    """Create an environment and start building it from a description. Prints its id.

    An agent reads the company the description is about and builds that company's app. A failed
    build is never started again: delete the environment and create it again.

    \b
    Examples:
      layerlens env create --type zendesk --name support-desk --description-file ci/environment.md --if-not-exists --wait
      layerlens env create --type zendesk --name support-desk --description "An outdoor-gear retailer's support desk"
    """
    if description is not None and description_file is not None:
        raise click.UsageError("pass --description or --description-file, not both")
    if description_file is not None:
        with open(description_file, encoding="utf-8") as f:
            description = f.read()
    if description is None:
        raise click.UsageError("pass --description or --description-file: every environment starts from a description")
    envs = env_client(ctx).environments
    created = envs.create(name, world_type, description=description, if_not_exists=if_not_exists)
    verb = "Reusing" if created.reused else "Created"
    building = created.job is not None and created.job.status not in JOB_TERMINAL_STATUSES
    progress(f"{verb} {created.name} ({created.id})" + (", building" if building else ""))
    if wait:
        ready = envs.wait_until_ready(created.id, timeout_seconds=timeout)
        progress(f"{ready.name} is ready: version {ready.environment_version}, {counted(ready.tool_count, 'tool')}")
        if json_output(ctx):
            # The create reply's build still reads "pending": print the environment and the build
            # as the wait left them, under the reply's own keys.
            job = envs.jobs.get(ready.id, created.job.id) if created.job is not None else None
            emit_json({"environment": ready, "job": job, "reused": created.reused})
            return
    if json_output(ctx):
        emit_json(created)
    else:
        click.echo(created.id)


@click.command("status", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@env_errors
def status(ctx: click.Context, environment: str) -> None:
    """Show the build: building, ready or failed, with its steps.

    \b
    Examples:
      layerlens env status support-desk
      layerlens --format json env status support-desk
    """
    envs = env_client(ctx).environments
    found = envs.get(environment)
    build = envs.build_status(found.id)
    if json_output(ctx):
        emit_json(build)
        return
    click.echo(f"{found.name}  {found.type}  {build.status}")
    _print_build(build)


def _print_build(build: EnvironmentBuild) -> None:
    for step in build.steps:
        mark = _STEP_MARKS.get(step.state, " ")
        detail = f"  {step.detail}" if step.detail else ""
        click.echo(f"  {mark} {step.name}{detail}")
    counts = [f"{n} {label}" for n, label in ((build.tools, "tools"), (build.records, "records")) if n is not None]
    if build.answer_key is not None:
        counts.append(f"{build.answer_key} answer-key facts")
    if counts:
        click.echo("  " + ", ".join(counts))
    if build.refused_because:
        click.echo(f"  refused: {build.refused_because}")


@click.command("wait", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.option("--timeout", default=DEFAULT_TIMEOUT, type=DURATION, help="Default 30m.")
@env_errors
def wait(ctx: click.Context, environment: str, timeout: float) -> None:
    """Block until the build finishes. Exits 0 when ready, 11 when it failed or timed out.

    \b
    Examples:
      layerlens env wait support-desk --timeout 20m
    """
    ready = env_client(ctx).environments.wait_until_ready(environment, timeout_seconds=timeout)
    if json_output(ctx):
        emit_json(ready)
    else:
        click.echo(f"{ready.name} is ready: version {ready.environment_version}, {counted(ready.tool_count, 'tool')}")


@click.command("tools", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@env_errors
def tools(ctx: click.Context, environment: str) -> None:
    """List the environment's tools.

    \b
    Examples:
      layerlens env tools support-desk
    """
    listed = env_client(ctx).environments.tools(environment)
    if json_output(ctx):
        emit_json(listed)
        return
    if not listed.tools:
        progress("This environment has no tools yet.")
        return
    rows = [{"name": t.name, "path": _real_path(t), "summary": t.summary} for t in listed.tools]
    columns: List[Tuple[str, str]] = [("name", "Tool"), ("summary", "Summary")]
    if any(row["path"] for row in rows):
        # A composite's tools are its apps' under aliases, which no real service's URLs describe:
        # the API sends them without a method and path.
        columns.insert(1, ("path", "Real path"))
    click.echo(format_table(rows, columns, 60))


def _real_path(tool: Tool) -> str:
    return f"{tool.method} {tool.path}".strip()


@click.command("reset", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@env_errors
def reset(ctx: click.Context, environment: str) -> None:
    """Clear the live state back to the minted world. Runs never touch it.

    \b
    Examples:
      layerlens env reset support-desk
    """
    result = env_client(ctx).environments.reset_state(environment)
    if json_output(ctx):
        emit_json(result)
    else:
        click.echo("The live state is reset." if result.deleted else "The live state was already the minted world.")


@click.command("revert", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.option("--to-version", required=True, type=click.IntRange(min=1), help="The version to serve.")
@env_errors
def revert(ctx: click.Context, environment: str, to_version: int) -> None:
    """Serve an earlier version of the world.

    \b
    Examples:
      layerlens env revert support-desk --to-version 3
    """
    ref = env_client(ctx).environments.revert(environment, to_version=to_version)
    if json_output(ctx):
        emit_json(ref)
    else:
        click.echo(f"{ref.name} now serves version {ref.current_version}.")


@click.command("delete", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
@env_errors
def delete(ctx: click.Context, environment: str, yes: bool) -> None:
    """Delete the environment. Its results, traces and attempts stay readable.

    \b
    Examples:
      layerlens env delete support-desk --yes
    """
    confirm(ctx, yes, f"Delete environment {environment}?")
    envs = env_client(ctx).environments
    environment_id = envs.resolve(environment)
    envs.delete(environment_id)
    if json_output(ctx):
        emit_json({"id": environment_id, "deleted": True})
    else:
        click.echo(f"Deleted {environment} ({environment_id}).")
