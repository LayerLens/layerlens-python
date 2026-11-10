"""``layerlens env run list`` and ``layerlens env run compare``: find runs, and compare two (LAY-4432)."""

from __future__ import annotations

import sys
import functools
import traceback
from typing import Any, Dict, List, Tuple, Callable, Optional
from datetime import datetime, timezone

import click

from ._common import (
    EXIT_OK,
    progress,
    emit_json,
    env_client,
    env_errors,
    json_output,
    api_error_text,
)
from ..._command import LayerLensCommand
from ..._formatter import format_table
from ...._exceptions import APIStatusError, LayerLensError
from ....models.environment import RunSummary, RunComparison
from ....models._run_reports import tally, points
from ....resources.environments import Environments
from ....resources.environments._shared import InvalidArgument

# `env run compare` exits as `evaluations compare` and diff(1) do, so that a CI step can tell
# "the candidate regressed" from "this step could not compare" by the exit code alone.
EXIT_NO_REGRESSION = EXIT_OK
EXIT_REGRESSION = 1
EXIT_NOT_COMPARED = 2

RUN_KINDS = ("external", "rollout")
RUN_STATUSES = ("pending", "in_progress", "success", "failure", "cancelled")

# The API's page size, at most.
_PAGE_SIZE = 50
_MAX_LISTED = 1000


@click.command("list", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.option("--label", default=None, help="Only runs started with exactly this --label.")
@click.option("--agent-name", default=None, help="Only runs of this agent.")
@click.option("--kind", type=click.Choice(RUN_KINDS), default=None, help="external (your agent) or rollout (a model).")
@click.option("--status", type=click.Choice(RUN_STATUSES), default=None, help="Only runs that show this status.")
@click.option(
    "--limit", default=20, type=click.IntRange(1, _MAX_LISTED), show_default=True, help="How many runs to list."
)
@click.option("--cursor", default=None, help="Go on from where a listing stopped (it prints the cursor).")
@env_errors
def list_runs(
    ctx: click.Context,
    environment: str,
    label: Optional[str],
    agent_name: Optional[str],
    kind: Optional[str],
    status: Optional[str],
    limit: int,
    cursor: Optional[str],
) -> None:
    """An environment's runs, newest first.

    \b
    Examples:
      layerlens env run list support-desk --label main --status success --limit 1
      layerlens --format json env run list support-desk --agent-name support-agent
    """
    envs = env_client(ctx).environments
    environment_id = envs.resolve(environment)
    runs: List[RunSummary] = []
    next_cursor = cursor
    while True:
        # Never more than is still wanted, so no page is cut and its cursor stays right.
        page = envs.runs.list(
            environment_id, label=label, agent_name=agent_name, kind=kind, status=status,
            limit=min(_PAGE_SIZE, limit - len(runs)), cursor=next_cursor,
        )  # fmt: skip
        runs.extend(page.runs)
        next_cursor = page.next_cursor
        if next_cursor is None or len(runs) >= limit:
            break
    if json_output(ctx):
        emit_json({"runs": runs, "next_cursor": next_cursor})
        return
    if not runs:
        click.echo("No runs.")
    else:
        click.echo(format_table([_row(run) for run in runs], _COLUMNS))
    if next_cursor is not None:
        progress(f"More runs: add --cursor {next_cursor}")


_COLUMNS = [
    ("run_id", "Run"),
    ("created", "Started"),
    ("kind", "Kind"),
    ("agent", "Agent"),
    ("label", "Label"),
    ("status", "Status"),
    ("passed", "Passed"),
    ("pass_rate", "Pass rate"),
]


def _row(run: RunSummary) -> Dict[str, Any]:
    return {
        "run_id": run.run_id,
        "created": _when(run.created_at),
        "kind": run.kind,
        "agent": run.agent_name or run.model or "-",
        "label": run.label or "-",
        "status": run.status,
        "passed": f"{run.passed}/{run.out_of}",
        "pass_rate": _percent(run.pass_rate),
    }


def _percent(rate: Optional[float]) -> str:
    return "-" if rate is None else f"{rate * 100:.1f}%"


def _when(at: Optional[datetime]) -> str:
    return "-" if at is None else at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _line(run: RunSummary) -> str:
    who = run.agent_name or run.model or run.run_id
    return f"{who}: {run.passed}/{run.out_of} passed ({_percent(run.pass_rate)}); status {run.status}"


class NotCompared(click.ClickException):
    """The runs could not be compared. click prints it as one ``Error:`` line, and exits 2."""

    exit_code = EXIT_NOT_COMPARED


class SummaryNotWritten(click.ClickException):
    """The runs were compared and nothing regressed, but ``--markdown`` could not be appended to.

    Exits 2: the step did not do all it was asked, so it does not pass, and only a regression
    exits 1. With a regression the command exits 1 all the same, and says the summary is missing.
    """

    exit_code = EXIT_NOT_COMPARED


def compare_errors(fn: Callable[..., Optional[RunComparison]]) -> Callable[..., None]:
    """Exit 2 for anything that is not a comparison, so that only a regression exits 1.

    ``env`` commands exit 1 on an API error; a CI step reading this command's 1 as a
    regression would block a pull request over a lost connection, or pass one whose
    comparison never ran. An interrupt is not a comparison either.
    """

    @functools.wraps(fn)
    @click.pass_context
    def wrapper(ctx: click.Context, *args: Any, **kwargs: Any) -> None:
        try:
            comparison = fn(ctx, *args, **kwargs)
        except (NotCompared, SummaryNotWritten, click.UsageError):
            raise
        except InvalidArgument as err:
            raise click.UsageError(str(err), ctx) from None
        except SystemExit as err:
            # fail() has printed why; it exits 1 for an error.
            if err.code in (None, EXIT_OK):
                raise
            sys.exit(EXIT_NOT_COMPARED)
        except (KeyboardInterrupt, click.Abort):
            raise NotCompared("interrupted; nothing was compared") from None
        except APIStatusError as err:
            raise NotCompared(api_error_text(err)) from None
        except LayerLensError as err:
            raise NotCompared(str(err)) from None
        except Exception as err:
            if ctx.obj.get("verbose"):
                traceback.print_exc()
            raise NotCompared(f"unexpected error: {err}") from err
        if comparison is not None and comparison.regression:
            sys.exit(EXIT_REGRESSION)

    return wrapper


@click.command("compare", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.argument("runs", nargs=-1, metavar="[BASELINE] CANDIDATE")
@click.option(
    "--baseline-label",
    default=None,
    metavar="LABEL",
    help="Compare with the newest run started with this --label that succeeded, of the candidate's agent.",
)
@click.option(
    "--baseline-agent-name",
    default=None,
    metavar="NAME",
    help="With --baseline-label: the baseline is this agent's run, not the candidate's agent's.",
)
@click.option(
    "--markdown",
    default=None,
    type=click.Path(dir_okay=False),
    help="Append a Markdown summary to this file (works with $GITHUB_STEP_SUMMARY).",
)
@compare_errors
def compare(
    ctx: click.Context,
    environment: str,
    runs: Tuple[str, ...],
    baseline_label: Optional[str],
    baseline_agent_name: Optional[str],
    markdown: Optional[str],
) -> Optional[RunComparison]:
    """Compare two runs task by task, and exit 1 if the candidate regressed.

    A task regressed when it passed on the baseline and fails on the candidate (fail,
    error or timeout). Pass two run ids, or the candidate's alone with --baseline-label:
    the baseline is then the newest run started with that label that succeeded, of the
    candidate's agent (or --baseline-agent-name) and kind, other than the candidate. Both
    runs must have succeeded.

    \b
    Exit codes: 0 no regression, 1 regression, 2 the runs could not be compared (a run
    not found, still running or not succeeded, no run with that label, an API error), or
    no task regressed but the --markdown file could not be written to.

    \b
    Examples:
      layerlens env run compare support-desk --baseline-label main "$RUN_ID" --markdown "$GITHUB_STEP_SUMMARY"
      layerlens env run compare support-desk BASELINE_RUN_ID CANDIDATE_RUN_ID
    """
    baseline_id, candidate_id = _run_ids(runs, baseline_label)
    if baseline_agent_name is not None and baseline_label is None:
        raise click.UsageError("--baseline-agent-name goes with --baseline-label")
    envs = env_client(ctx).environments
    environment_id = envs.resolve(environment)
    candidate = envs.runs.get(environment_id, candidate_id)
    _check_succeeded("candidate", candidate, environment)
    if baseline_id is None:
        assert baseline_label is not None
        agent = candidate.agent_name if baseline_agent_name is None else baseline_agent_name
        if agent is None:
            # Listing by label alone would make the newest run of any agent or model the baseline.
            raise NotCompared(
                "the candidate has no agent name, so the baseline cannot be found by label alone; "
                "pass --baseline-agent-name, or the baseline's run id"
            )
        baseline_id = _newest_labelled(envs, environment_id, baseline_label, agent, candidate)
    elif baseline_id == candidate_id:
        raise NotCompared("the baseline and the candidate are the same run; pass two runs")
    baseline = envs.runs.get(environment_id, baseline_id)
    _check_succeeded("baseline", baseline, environment)
    if baseline.agent_name != candidate.agent_name:
        progress(f"Note: the runs are of different agents: {baseline.agent_name!r} and {candidate.agent_name!r}")

    comparison = RunComparison.between(baseline, candidate)
    if json_output(ctx):
        emit_json(comparison)
    else:
        _print(comparison)
    unwritten = _append_summary(markdown, comparison) if markdown else None
    regressed = len(comparison.regressed)
    progress(f"Regression: {regressed} task{'s' if regressed != 1 else ''} regressed" if regressed else "No regression")
    if unwritten is not None:
        # The comparison is made, so its verdict is the exit code: a regression exits 1 whatever
        # else failed. It used to exit 2, "not compared", which a CI gate may let pass.
        if comparison.regression:
            click.echo(f"Error: {unwritten}", err=True)
        else:
            raise SummaryNotWritten(f"{unwritten}; the runs were compared and no task regressed")
    return comparison


def _append_summary(path: str, comparison: RunComparison) -> Optional[str]:
    """Append the comparison's Markdown to ``path``; None, or why it could not be."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(comparison.to_markdown())
    except OSError as err:
        return f"the Markdown summary could not be appended to {path}: {err.strerror or err}"
    progress(f"Markdown summary appended to {path}")
    return None


def _run_ids(runs: Tuple[str, ...], baseline_label: Optional[str]) -> Tuple[Optional[str], str]:
    if baseline_label is not None:
        if not baseline_label.strip():
            raise click.UsageError("--baseline-label is blank; name the label the baseline's run was started with")
        if len(runs) != 1:
            raise click.UsageError("with --baseline-label, pass only the candidate's run id")
        return None, runs[0]
    if len(runs) != 2:
        raise click.UsageError("pass BASELINE and CANDIDATE run ids, or --baseline-label LABEL and CANDIDATE")
    return runs[0], runs[1]


def _check_succeeded(role: str, run: RunSummary, environment: str) -> None:
    if run.status == "success":
        return
    if not run.finished:
        raise NotCompared(
            f"the {role} run {run.run_id} is still {run.status}; wait for it with "
            f"`layerlens env run wait {environment} {run.run_id}`, then compare"
        )
    why = f" ({run.error})" if run.error else ""
    raise NotCompared(f"the {role} run {run.run_id} did not succeed: it is {run.status}{why}, so it says "
                      "nothing reliable about the agent")  # fmt: skip


def _newest_labelled(
    envs: Environments, environment_id: str, label: str, agent: Optional[str], candidate: RunSummary
) -> str:
    """The newest run with ``label`` that succeeded, of ``agent`` and the candidate's kind, other than the candidate."""
    for run in envs.runs.iter(
        environment_id, label=label, agent_name=agent, kind=candidate.kind, status="success",
        page_size=2,
    ):  # fmt: skip
        if run.run_id != candidate.run_id:
            progress(f"Baseline: run {run.run_id}, the newest run labelled {label!r} that succeeded "
                     f"(started {_when(run.created_at)})")  # fmt: skip
            return run.run_id
    whose = f" of agent {agent!r}" if agent else ""
    raise NotCompared(
        f"no {candidate.kind} run{whose} labelled {label!r} has succeeded in this environment, other than the "
        "candidate; run the job on that label first, name the baseline's agent with --baseline-agent-name, or "
        "pass the baseline's run id"
    )


def _print(comparison: RunComparison) -> None:
    for role, run in (("Baseline ", comparison.baseline), ("Candidate", comparison.candidate)):
        label = f"label {run.label}  " if run.label else ""
        click.echo(f"{role}  run {run.run_id}  {label}{_line(run)}")
    base, cand = comparison.baseline.pass_rate, comparison.candidate.pass_rate
    click.echo(f"Pass rate  {points(comparison.pass_rate_delta)} points ({_percent(base)} -> {_percent(cand)})")
    if comparison.environment_versions_differ:
        click.echo(
            f"Note: the runs were served different environment versions: {comparison.baseline.environment_version} "
            f"and {comparison.candidate.environment_version}"
        )
    if comparison.regressed:
        n = len(comparison.regressed)
        click.echo("")
        click.echo(f"{n} task{'s' if n != 1 else ''} passed on the baseline and fail{'' if n != 1 else 's'} "
                   "on the candidate:")  # fmt: skip
        rows = [
            {
                "task": change.task_name or change.task_id,
                "verdict": change.candidate.verdict,
                "evidence": " ".join((change.candidate.evidence or "").split()),
                "trace_id": change.candidate.trace_id,
            }
            for change in comparison.regressed
        ]
        columns = [("task", "Task"), ("verdict", "Verdict"), ("evidence", "Evidence"), ("trace_id", "Trace")]
        click.echo(format_table(rows, columns, max_col_width=80))
    if comparison.regressed:
        click.echo("")
    click.echo(tally(comparison))
