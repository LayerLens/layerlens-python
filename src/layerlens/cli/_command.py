"""The root options, accepted before the subcommand and after it (LAY-4250).

``--api-key``, ``--api-key-file``, ``--host``, ``--port``, ``--format``, ``-v`` and
``-q`` are options of the root group, so click only accepted them before the subcommand:
``layerlens judge result abc123 --format json`` failed with "No such option".
Every command built from :class:`LayerLensCommand` declares them as well. A value
given after the subcommand wins over one given before it, as the later and more
specific of the two.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Tuple, TypeVar, Callable, Optional

import click

from .._env import API_KEY_ENV

_OptionSpec = Tuple[Tuple[str, ...], Dict[str, Any]]
_F = TypeVar("_F", bound=Callable[..., Any])


def _record(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
    """Store a root option given after the command where commands read the root
    options, ``ctx.obj``. One the user did not repeat there is ``None`` (``False``
    for a flag) and leaves the root value alone."""
    if value is None or value is False:
        return value
    name = str(param.name)
    obj = ctx.ensure_object(dict)
    obj[name] = value
    if name == "api_key":
        obj["api_key_source"] = "--api-key"
    elif name in ("host", "port"):
        obj["base_url"] = base_url_for(obj.get("host"), obj.get("port"))
    return value


def _option_specs(*, root: bool) -> List[_OptionSpec]:
    """The root options. After a subcommand they have no defaults and no
    environment variable, and they never reach the command's callback: they are
    recorded in ``ctx.obj`` as they are parsed, so the command runs the same however
    it is invoked (``ctx.invoke`` included)."""
    after: Dict[str, Any] = {} if root else {"expose_value": False, "callback": _record}
    return [
        (
            ("--api-key",),
            {
                "envvar": API_KEY_ENV if root else None,
                "default": None,
                "help": f"API key for authentication (default: ${API_KEY_ENV}).",
                **after,
            },
        ),
        (
            ("--api-key-file",),
            {
                "default": None,
                "metavar": "PATH",
                "help": "Read the API key from this file, or from standard input for -.",
                **after,
            },
        ),
        (("--host",), {"default": None, "help": "API host (e.g. api.layerlens.ai).", **after}),
        (("--port",), {"default": None, "type": int, "help": "API port.", **after}),
        (
            ("--format", "output_format"),
            {
                "type": click.Choice(["table", "json"]),
                "default": "table" if root else None,
                "help": "Output format (default: table).",
                **after,
            },
        ),
        (("--verbose", "-v"), {"is_flag": True, "default": False, "help": "Enable verbose/debug output.", **after}),
        (("--quiet", "-q"), {"is_flag": True, "default": False, "help": "Suppress the startup banner.", **after}),
    ]


def root_options(f: _F) -> _F:
    """Decorate the root group with the root options."""
    for decls, attrs in reversed(_option_specs(root=True)):
        f = click.option(*decls, **attrs)(f)
    return f


def base_url_for(host: Optional[str], port: Optional[int]) -> Optional[str]:
    """The API base URL for ``--host``/``--port``, or ``None`` without a host."""
    if host is None:
        return None
    scheme = "https" if port in (None, 443) else "http"
    if port and port not in (80, 443):
        return f"{scheme}://{host}:{port}/api/v1"
    return f"{scheme}://{host}/api/v1"


def _stderr_is_terminal() -> bool:
    return sys.stderr.isatty()


def _show_banner(obj: Dict[str, Any]) -> None:
    if obj.get("quiet") or not _stderr_is_terminal():
        return
    from ._banner import banner
    from .._version import __version__

    click.echo(banner(__version__), err=True)


class LayerLensCommand(click.Command):
    """A command that also accepts the root options after its name."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        taken = {o for p in self.params for o in (p.name, *p.opts, *p.secondary_opts) if o}
        for decls, attrs in _option_specs(root=False):
            option = click.Option(list(decls), **attrs)
            clash = {o for o in (option.name, *option.opts) if o} & taken
            if clash:
                raise TypeError(f"command {self.name!r} declares {sorted(clash)}, which is a root option")
            self.params.append(option)

    def invoke(self, ctx: click.Context) -> Any:
        obj = ctx.ensure_object(dict)
        # Checked here, once the options on both sides of the command are parsed.
        if obj.get("port") is not None and obj.get("host") is None:
            raise click.UsageError("--port needs --host.", ctx)
        # Shown here rather than by the root group: only now is a trailing -q known.
        _show_banner(obj)
        return super().invoke(ctx)


class LayerLensGroup(click.Group):
    """A group whose commands and subgroups accept the root options after their name."""

    command_class = LayerLensCommand
    group_class = type  # subgroups are LayerLensGroups too
