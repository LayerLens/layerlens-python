"""CLI entry points for local replays (``layerlens replay ...``)."""

from __future__ import annotations

import sys
import json
from typing import Callable, cast

import click

from ...replay import ReplayStatus, ReplayRequest, ReplayController
from .._client import get_client, handle_errors, api_error_text
from .._command import LayerLensGroup
from ..._exceptions import NotFoundError, LayerLensError, BadRequestError
from .._safe_loader import load_callable
from ...models.trace import Trace


def _load_callable(spec: str) -> Callable[..., Trace]:
    """Resolve ``module.submodule:attr`` into a callable (allowlist-guarded).

    See :mod:`layerlens.cli._safe_loader`: the loader refuses any stdlib/builtin
    root BEFORE importing, so a malicious ``--replay-fn`` (``posix:system``,
    ``os:system``, an import-side-effect module) cannot execute.
    """
    return cast(Callable[..., Trace], load_callable(spec, param_hint="--replay-fn"))


@click.group(cls=LayerLensGroup)
def replay() -> None:
    """Replay traces locally with overrides."""


@replay.command("run")
@click.option("--trace-id", required=True)
@click.option("--trace-file", type=click.Path(exists=True, dir_okay=False))
@click.option("--replay-fn", default=None, help="Callable 'module:attr' that replays a trace.")
@click.option("--model-override", default=None)
@click.option("--input-override", multiple=True, help="KEY=VALUE (repeatable).")
@click.option("--prompt-override", multiple=True, help="KEY=VALUE (repeatable).")
@click.pass_context
@handle_errors
def run(
    ctx: click.Context,
    trace_id: str,
    trace_file: str | None,
    replay_fn: str | None,
    model_override: str | None,
    input_override: tuple,
    prompt_override: tuple,
) -> None:
    """Replay one trace with overrides and print the result.

    The trace is --trace-file (as `layerlens trace export` writes it) or, without it, the
    stored trace --trace-id names, read from LayerLens. Exits 1 when the replay does not
    complete, 2 when --trace-id names no stored trace.
    """
    # Before the trace is read: a --replay-fn the loader refuses never costs a request.
    fn = _load_callable(replay_fn) if replay_fn else _echo_replay
    if trace_file:
        with open(trace_file, "r", encoding="utf-8") as fh:
            trace_payload = json.load(fh)
        original = Trace(**trace_payload)
        if original.id != trace_id:
            # The replay is of the file's trace, and its result's original_trace_id is that trace's.
            click.echo(
                f"Warning: --trace-file holds trace {original.id}, not {trace_id}; the result names {original.id}.",
                err=True,
            )
    else:
        original = _stored_trace(ctx, trace_id)

    request = ReplayRequest(
        trace_id=trace_id,
        model_override=model_override,
        input_overrides=dict(_kv(input_override)),
        prompt_overrides=dict(_kv(prompt_override)),
    )

    controller = ReplayController(fn)
    result = controller.run(original, request)
    click.echo(result.model_dump_json(indent=2))
    if result.status != ReplayStatus.COMPLETED:
        click.echo(f"Error: the replay {result.status.value}: {result.error or 'no reason given'}", err=True)
        sys.exit(1)


def _stored_trace(ctx: click.Context, trace_id: str) -> Trace:
    """The trace ``trace_id`` names, read from LayerLens (BUG-07: it used to be an empty one)."""
    client = get_client(ctx)
    try:
        trace = client.traces.get(trace_id, strict=True)
    except (NotFoundError, BadRequestError) as err:
        raise click.UsageError(
            f"--trace-id {trace_id!r} names no stored trace: {api_error_text(err)}. Pass the id of a trace "
            "in this project (`layerlens trace list`), or --trace-file with one `layerlens trace export` wrote."
        ) from None
    if trace is None:  # strict=True raises instead; kept for the type
        raise LayerLensError(f"trace {trace_id} could not be read")
    return trace


def _kv(pairs: tuple) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(f"expected KEY=VALUE (got {pair!r})")
        k, v = pair.split("=", 1)
        out.append((k, v))
    return out


def _echo_replay(trace: Trace, _: ReplayRequest) -> Trace:
    """Fallback replay that returns the input trace unchanged."""
    return trace
