from __future__ import annotations

import json
from typing import Any, Dict, Tuple, Optional

import click

from ._common import (
    DURATION,
    DEFAULT_TIMEOUT,
    confirm,
    progress,
    emit_json,
    env_client,
    env_errors,
    json_output,
)
from ..._command import LayerLensCommand
from ..._formatter import format_table, format_single
from ....models.environment import JOB_TERMINAL_STATUSES, WorldType, Environment, EnvironmentBuild

_STEP_MARKS = {"done": "✓", "running": "…", "failed": "✗"}


@click.command("world-types", cls=LayerLensCommand)
@env_errors
def world_types(ctx: click.Context) -> None:
    """List the services an environment can simulate, with their config knobs.

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
        {"type": t.type, "title": t.title, "tools": t.tool_count, "knobs": _knobs(t)}
        for t in sorted(types, key=lambda t: t.type)
    ]
    click.echo(
        format_table(rows, [("type", "Type"), ("title", "Title"), ("tools", "Tools"), ("knobs", "Config knobs")], 60)
    )


def _knobs(world_type: WorldType) -> str:
    if world_type.built_by_agent:
        return "none: an agent builds it from the description"
    parts = []
    for name, field in sorted(world_type.config_schema.items()):
        bounds = ""
        if field.minimum is not None or field.maximum is not None:
            low = "" if field.minimum is None else f"{field.minimum:g}"
            high = "" if field.maximum is None else f"{field.maximum:g}"
            bounds = f" {low}-{high}"
        elif field.enum:
            bounds = " " + "|".join(str(v) for v in field.enum)
        default = "" if field.default is None else f" (default {field.default})"
        parts.append(f"{name}{bounds}{default}")
    return ", ".join(parts) or "-"


@click.command("list", cls=LayerLensCommand)
@click.option("--search", default=None, help="Only environments whose name contains this.")
@click.option(
    "--hide-members", is_flag=True, default=False, help="Leave out environments that are members of a composite."
)
@env_errors
def list_environments(ctx: click.Context, search: Optional[str], hide_members: bool) -> None:
    """List the project's environments.

    \b
    Examples:
      layerlens env list
      layerlens env list --search support --hide-members
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
@click.option("--name", required=True, help="1-120 characters, unique in the organization.")
@click.option(
    "--description", default=None, help="What the world should contain, in plain words. This or --description-file."
)
@click.option(
    "--description-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Read the description from a file.",
)
@click.option("--config", "config", multiple=True, metavar="KEY=VALUE", help="Set a config knob. Repeatable.")
@click.option("--seed", default=None, type=int, help="Default 1.")
@click.option(
    "--if-not-exists",
    is_flag=True,
    default=False,
    help="Reuse an environment of this name created from the same inputs; fail if it differs.",
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
    config: Tuple[str, ...],
    seed: Optional[int],
    if_not_exists: bool,
    wait: bool,
    timeout: float,
) -> None:
    """Create an environment and start minting its world from a description. Prints its id.

    \b
    Examples:
      layerlens env create --type zendesk --name support-desk --description-file ci/environment.md --if-not-exists --wait
      layerlens env create --type zendesk --name support-desk --description "A small support desk" --config tickets=40
    """
    if description is not None and description_file is not None:
        raise click.UsageError("pass --description or --description-file, not both")
    if description_file is not None:
        with open(description_file, encoding="utf-8") as f:
            description = f.read()
    if description is None:
        raise click.UsageError("pass --description or --description-file: every environment starts from a description")
    envs = env_client(ctx).environments
    created = envs.create(
        name,
        world_type,
        description=description,
        config=_config(config) if config else None,
        seed=seed,
        if_not_exists=if_not_exists,
    )
    verb = "Reusing" if created.reused else "Created"
    building = created.job is not None and created.job.status not in JOB_TERMINAL_STATUSES
    progress(f"{verb} {created.name} ({created.id})" + (", building" if building else ""))
    if created.unused:
        progress(f"Note: {created.unused}")
    if wait:
        ready = envs.wait_until_ready(created.id, timeout_seconds=timeout)
        progress(f"{ready.name} is ready: version {ready.environment_version}, {ready.tool_count} tools")
    if json_output(ctx):
        emit_json(created)
    else:
        click.echo(created.id)


def _config(pairs: Tuple[str, ...]) -> Dict[str, Any]:
    """``KEY=VALUE`` knobs; a VALUE that reads as JSON (a number, true, a list) is sent as that."""
    config: Dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise click.BadParameter(f"{pair!r} is not KEY=VALUE", param_hint="--config")
        try:
            config[key.strip()] = json.loads(value)
        except ValueError:
            config[key.strip()] = value
    return config


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
        click.echo(f"{ready.name} is ready: version {ready.environment_version}, {ready.tool_count} tools")


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
    rows = [{"name": t.name, "path": f"{t.method} {t.path}".strip(), "summary": t.summary} for t in listed.tools]
    click.echo(format_table(rows, [("name", "Tool"), ("path", "Real path"), ("summary", "Summary")], 60))


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
