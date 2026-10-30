"""``layerlens env``: environments from a terminal or a CI job, over the Automation API."""

from __future__ import annotations

import click

from .runs import run
from .tasks import tasks
from ..._command import LayerLensGroup
from .environments import get, wait, reset, tools, create, delete, revert, status, world_types, list_environments


@click.group("env", cls=LayerLensGroup)
def env() -> None:
    """Create environments, run your agent or a model against them, and gate a build on the result.

    Needs an Automation API key. Wherever a command takes ENV, pass the environment's
    id or its name.

    \b
    Exit codes: 0 success, 1 error, 2 usage, 10 below --min-pass-rate, 11 the run or build did not finish.

    \b
    Examples:
      layerlens env create --type zendesk --name support-desk --description-file ci/environment.md --if-not-exists --wait
      layerlens env run exec support-desk --tasks-file ci/tasks.jsonl --agent-name support-agent --min-pass-rate 0.9 -- python ci/agent.py
    """


for command in (world_types, list_environments, get, create, status, wait, tools, reset, revert, delete, tasks, run):
    env.add_command(command)

__all__ = ["env"]
