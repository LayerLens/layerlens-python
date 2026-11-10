"""What every ``layerlens env`` command shares: exit codes, errors, the client, output."""

from __future__ import annotations

import re
import sys
import json
import math
import signal
import functools
import threading
import traceback
from types import FrameType
from typing import Any, List, Callable, Iterator, NoReturn, Optional
from contextlib import contextmanager

import click

from ...._env import API_KEY_ENV
from ..._client import build_client, api_error_text, resolve_api_key
from ...._client import LayerLens
from ...._exceptions import (
    APIStatusError,
    LayerLensError,
    APIConnectionError,
    EnvironmentBuildError,
)
from ....models.environment import RunResult
from ....resources.environments._shared import InvalidArgument

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_BELOW_MIN_PASS_RATE = 10
EXIT_NOT_FINISHED = 11

DEFAULT_TIMEOUT = "30m"


def env_client(ctx: click.Context, *, key_on_command_line: bool = True) -> LayerLens:
    """A client on the API key. ``layerlens env`` takes an API key only, never a login session.

    Most commands need the key's Automation access; ``env list``, ``env tools`` and ``env reset``
    run with Standard access too, as the API serves an agent those routes.
    """
    resolved = resolve_api_key(ctx)
    if resolved is None:
        ways = "--api-key-file PATH or --api-key" if key_on_command_line else "--api-key-file PATH"
        fail(
            f"layerlens env needs an API key: set {API_KEY_ENV}, or pass {ways}. A key with Standard access "
            "runs only env list, env tools and env reset; every other command needs Automation access "
            "(Organization Settings → API Keys)."
        )
    return build_client(ctx, resolved.value, use_bearer_auth=False)


def confirm(ctx: click.Context, yes: bool, question: str) -> None:
    """Ask ``question`` unless ``--yes``; refused when standard input holds the key instead."""
    if yes:
        return
    if ctx.obj.get("api_key_file") == "-":
        raise click.UsageError(
            "--api-key-file - reads the key from standard input, which this command's confirmation would read "
            "too: pass --yes"
        )
    click.confirm(question, abort=True, err=True)


@contextmanager
def sigterm_as_interrupt() -> Iterator[None]:
    """A CI runner stops a job with SIGTERM: treat it as Ctrl-C, so the run is cancelled."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupt(signum: int, frame: Optional[FrameType]) -> None:  # noqa: ARG001
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextmanager
def interrupts_held_off() -> Iterator[None]:
    """Ignore Ctrl-C and SIGTERM, so a second one cannot cut short the cancel the first one started."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {number: signal.signal(number, signal.SIG_IGN) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def fail(message: str, code: int = EXIT_ERROR) -> NoReturn:
    click.echo(f"Error: {message}", err=True)
    sys.exit(code)


def env_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Exit codes for ``layerlens env``: 1 for an error, 2 for usage, 11 for a build that did not finish."""

    @functools.wraps(fn)
    @click.pass_context
    def wrapper(ctx: click.Context, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(ctx, *args, **kwargs)
        except (click.exceptions.Exit, click.ClickException, click.Abort):
            raise
        except InvalidArgument as err:
            raise click.UsageError(str(err), ctx) from None
        except EnvironmentBuildError as err:
            fail(str(err), EXIT_NOT_FINISHED)
        except TimeoutError as err:
            fail(str(err), EXIT_NOT_FINISHED)
        except APIStatusError as err:
            fail(api_error_text(err))
        except LayerLensError as err:
            fail(str(err))
        except Exception as err:
            if ctx.obj.get("verbose"):
                traceback.print_exc()
            fail(f"unexpected error: {err}")

    return wrapper


def json_output(ctx: click.Context) -> bool:
    return bool(ctx.obj.get("output_format") == "json")


def emit_json(value: Any) -> None:
    click.echo(json.dumps(_plain(value), indent=2))


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def progress(message: str) -> None:
    """A line about work in progress: always stderr, so stdout holds only the result."""
    click.echo(message, err=True)


_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?")


class Duration(click.ParamType):
    """``90``, ``90s``, ``10m``, ``1h`` or ``1h30m``, in seconds."""

    name = "duration"

    def convert(self, value: Any, param: Optional[click.Parameter], ctx: Optional[click.Context]) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        try:
            seconds = float(text)
        except ValueError:
            match = _DURATION.fullmatch(text) if text else None
            groups = match.groups() if match is not None else ()
            if not any(groups):
                self.fail(f"{value!r} is not a duration such as 90s, 10m or 1h30m", param, ctx)
            hours, minutes, secs = (float(g) if g else 0.0 for g in groups)
            seconds = hours * 3600 + minutes * 60 + secs
        if seconds <= 0:
            self.fail(f"{value!r} must be longer than zero", param, ctx)
        return seconds


DURATION = Duration()


def task_names(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    names = [n.strip() for n in value.split(",") if n.strip()]
    if not names:
        raise click.BadParameter("name at least one task", param_hint="--tasks")
    return names


def task_options(f: Callable[..., Any]) -> Callable[..., Any]:
    """``--tasks-file``, ``--set`` and ``--tasks``: which tasks a run takes."""
    f = click.option("--tasks", "tasks", default=None, help="Run only these tasks of the file or set (NAME,…).")(f)
    f = click.option("--set", "set_name", default=None, help="Run a stored task set.")(f)
    f = click.option(
        "--tasks-file",
        default=None,
        type=click.Path(exists=True, dir_okay=False),
        help="Run the tasks in this JSONL file, stored with this run only.",
    )(f)
    return f


def check_task_choice(tasks_file: Optional[str], set_name: Optional[str]) -> None:
    if (tasks_file is None) == (set_name is None):
        raise click.UsageError("choose the tasks with exactly one of --tasks-file or --set")


def report_options(f: Callable[..., Any]) -> Callable[..., Any]:
    f = click.option(
        "--markdown",
        default=None,
        type=click.Path(dir_okay=False),
        help="Append a Markdown summary to this file (works with $GITHUB_STEP_SUMMARY).",
    )(f)
    f = click.option(
        "--junit", default=None, type=click.Path(dir_okay=False), help="Write a JUnit XML report, one case per task."
    )(f)
    return f


def _a_rate(ctx: click.Context, param: click.Parameter, value: Optional[float]) -> Optional[float]:  # noqa: ARG001
    if value is not None and math.isnan(value):
        raise click.BadParameter("is not a number; give a pass rate from 0 to 1, such as 0.9")
    return value


def min_pass_rate_option(f: Callable[..., Any]) -> Callable[..., Any]:
    return click.option(
        "--min-pass-rate",
        default=None,
        type=click.FloatRange(0, 1),
        callback=_a_rate,
        help="Exit 10 when the run's pass rate is below this (0-1).",
    )(f)


def idempotency_option(what: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """``--idempotency-key``: the same key gets the same ``what`` back instead of starting another."""
    return click.option(
        "--idempotency-key",
        default=None,
        metavar="KEY",
        help=f"The key a start that failed printed: get back the {what} it may have started, not another.",
    )


def lost_start(err: LayerLensError, what: str, key: str) -> str:
    """Why a start whose reply never came, or came as a server error, may have started anyway, and how to get it back."""
    text = api_error_text(err) if isinstance(err, APIStatusError) else str(err).rstrip(".")
    return (
        f"{text}, so whether the {what} started is not known. Run this command again with "
        f"--idempotency-key {key} to get it back if it did, instead of starting another."
    )


def interrupted_start(what: str, key: str) -> str:
    return (
        f"interrupted while starting the {what}, so whether it started is not known. Run this command again "
        f"with --idempotency-key {key} to get it back if it did, instead of starting another."
    )


def start_not_known(err: LayerLensError) -> bool:
    """Whether a keyed start that failed with ``err`` may have started all the same."""
    return isinstance(err, APIConnectionError) or (isinstance(err, APIStatusError) and err.status_code >= 500)


def write_reports(run: RunResult, junit: Optional[str], markdown: Optional[str]) -> bool:
    """Write the reports asked for; False, once each failure is printed, when one could not be.

    A report that cannot be written (a missing directory, no permission) does not stop the other,
    nor the gate: the caller decides the exit code from the run, and from this only when the run
    passes it.
    """
    written = True
    if junit:
        try:
            run.to_junit(junit)
        except OSError as err:
            click.echo(f"Error: the JUnit report could not be written to {junit}: {err.strerror or err}", err=True)
            written = False
        else:
            progress(f"JUnit report written to {junit}")
    if markdown:
        try:
            with open(markdown, "a", encoding="utf-8") as f:
                f.write(run.to_markdown())
        except OSError as err:
            click.echo(
                f"Error: the Markdown summary could not be written to {markdown}: {err.strerror or err}", err=True
            )
            written = False
        else:
            progress(f"Markdown summary appended to {markdown}")
    return written


def gate(run: RunResult, min_pass_rate: Optional[float]) -> int:
    """The exit code a finished run earns: 11 when it did not succeed, 10 below ``min_pass_rate``."""
    if run.status != "success":
        return EXIT_NOT_FINISHED
    if min_pass_rate and (run.out_of == 0 or run.passed < min_pass_rate * run.out_of - 1e-9):
        return EXIT_BELOW_MIN_PASS_RATE
    return EXIT_OK


def explain_gate(run: RunResult, min_pass_rate: Optional[float], code: int) -> None:
    if code == EXIT_NOT_FINISHED:
        progress(f"The run did not succeed: {run.status}" + (f" ({run.error})" if run.error else ""))
    elif code == EXIT_BELOW_MIN_PASS_RATE and run.out_of == 0:
        progress("No task's verdict counts toward the pass rate, so --min-pass-rate cannot be met")
    elif code == EXIT_BELOW_MIN_PASS_RATE and min_pass_rate is not None:
        progress(f"Pass rate {_percent(run.pass_rate)} is below --min-pass-rate {min_pass_rate * 100:.1f}%")


def _percent(rate: Optional[float]) -> str:
    return "-" if rate is None else f"{rate * 100:.1f}%"


def run_summary_line(run: RunResult) -> str:
    who = run.agent_name or run.model or run.run_id
    pending = f", {run.pending} pending" if run.pending else ""
    pending += f", {run.uncounted} not counted" if run.uncounted else ""
    return (
        f"{who}: {run.passed}/{run.out_of} passed ({_percent(run.pass_rate)}), "
        f"{run.failed} failed, {run.errored} errored, {run.timed_out} timed out{pending}; status {run.status}"
    )


def print_run(ctx: click.Context, run: RunResult) -> None:
    """A run on stdout: its JSON with --format json, else a summary and the tasks that did not pass."""
    if json_output(ctx):
        emit_json(run)
        return
    click.echo(f"run {run.run_id}  {run_summary_line(run)}")
    not_passed = [a for a in run.attempts if not a.passed and a.counted]
    if not not_passed:
        return
    from ..._formatter import format_table

    rows = [
        {
            "task": a.task_name or a.task_id,
            "verdict": a.verdict or "pending",
            "evidence": " ".join((a.evidence or "").split()),
            "trace_id": a.trace_id,
        }
        for a in not_passed
    ]
    click.echo(
        format_table(
            rows,
            [("task", "Task"), ("verdict", "Verdict"), ("evidence", "Evidence"), ("trace_id", "Trace")],
            max_col_width=80,
        )
    )


def finish_with(code: int) -> None:
    if code != EXIT_OK:
        sys.exit(code)
