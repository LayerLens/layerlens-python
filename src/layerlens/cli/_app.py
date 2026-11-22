from __future__ import annotations

import importlib.metadata as importlib_metadata

import click
from click import shell_completion

from ._command import LayerLensGroup, base_url_for, root_options
from .._version import __version__
from .commands.ci import ci
from .commands.env import env
from .commands.auth import login, logout, whoami
from .commands.bulk import bulk
from .commands.judge import judge
from .commands.trace import trace
from .commands.replay import replay
from .commands.scorer import scorer
from .commands.evaluate import evaluate
from .commands.synthetic import synthetic
from .commands.evaluations import evaluations
from .commands.integration import integration


@click.group(cls=LayerLensGroup)
@root_options
@click.version_option(version=__version__, prog_name="layerlens")
@click.pass_context
def cli(
    ctx: click.Context,
    api_key: str | None,
    api_key_file: str | None,
    host: str | None,
    port: int | None,
    output_format: str,
    verbose: bool,
    quiet: bool,
) -> None:
    """LayerLens CLI — manage traces, judges, evaluations, integrations, and more.

    The options below, except --version, also work at the end of the command.

    \b
    Examples:
      layerlens --format json judge get <judge-id>
      layerlens judge get <judge-id> --format json
    """
    ctx.ensure_object(dict)
    ctx.obj["api_key"] = api_key
    # Where the key came from, so `whoami` can say so.
    from_env = ctx.get_parameter_source("api_key") is click.core.ParameterSource.ENVIRONMENT
    ctx.obj["api_key_source"] = "LAYERLENS_API_KEY" if from_env else "--api-key"
    ctx.obj["api_key_file"] = api_key_file
    ctx.obj["output_format"] = output_format
    ctx.obj["verbose"] = verbose
    # The banner is shown by the command (see _command.LayerLensCommand), once a -q after it is known.
    ctx.obj["quiet"] = quiet
    ctx.obj["host"] = host
    ctx.obj["port"] = port
    ctx.obj["base_url"] = base_url_for(host, port)


# Core commands
cli.add_command(trace)
cli.add_command(judge)
cli.add_command(evaluate)
cli.add_command(integration)

# Additional commands
cli.add_command(scorer)
cli.add_command(bulk)
cli.add_command(ci)
cli.add_command(env)

# Local dataset / replay / synthetic workflows
cli.add_command(replay)
cli.add_command(synthetic)
cli.add_command(evaluations)

# Auth commands
cli.add_command(login)
cli.add_command(logout)
cli.add_command(whoami)


@cli.command("completion")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish", "powershell"]))
def completion(shell: str) -> None:
    """Print shell completion setup instructions.

    \b
    Examples:
      layerlens completion bash
      layerlens completion zsh
      layerlens completion fish
      layerlens completion powershell
    """
    import os

    # Detect which command name was used to invoke the CLI (`stratix` is the deprecated alias)
    prog = os.path.basename(os.environ.get("_", "layerlens"))
    if prog not in ("layerlens", "stratix"):
        prog = "layerlens"
    env_var = f"_{prog.upper()}_COMPLETE"

    if shell == "powershell":
        # click's own setup (its "Shell Completion" docs): ``powershell_source`` prints a whole
        # Register-ArgumentCompleter script, which the profile loads. The snippet used to register
        # a completer of its own that offered each line of that script as a completion (L5-41).
        if shell_completion.get_completion_class("powershell") is None:
            raise click.ClickException(
                "PowerShell completion needs click 8.5 or later (Python 3.10 or later); this "
                f"environment has click {_click_version()}. Upgrade click to use it."
            )
        snippet = (
            f"$env:{env_var} = 'powershell_source'\n{prog} | Out-String | Invoke-Expression\nRemove-Item Env:{env_var}"
        )
        print(f"Add this to your PowerShell profile (the file $PROFILE names):\n\n{snippet}")
        return
    instructions = {
        "bash": f'eval "$({env_var}=bash_source {prog})"',
        "zsh": f'eval "$({env_var}=zsh_source {prog})"',
        "fish": f"{env_var}=fish_source {prog} | source",
    }
    print(f"Add this to your shell profile:\n\n  {instructions[shell]}")


def _click_version() -> str:
    try:
        return importlib_metadata.version("click")
    except importlib_metadata.PackageNotFoundError:
        return "of an unknown version"
