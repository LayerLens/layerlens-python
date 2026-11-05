"""``layerlens ci report``: one summary, for a CI job page, of what a CI step names.

A report covers the benchmark evaluations, trace evaluations and environment runs it is
given by id. It never picks them for itself: "the project's latest" includes other
people's work, so it is only on offer behind ``--latest``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple, TypeVar, Callable, Optional, NamedTuple
from datetime import datetime, timezone
from functools import partial

import click

from ...models import RunResult, Evaluation, TraceEvaluation, EvaluationStatus, TraceEvaluationResultsResponse
from .._client import get_client, handle_errors, api_error_text
from .._output import echo_status
from ..._client import LayerLens
from .._command import LayerLensGroup
from ..._exceptions import APIStatusError, LayerLensError, AuthenticationError
from ...models._run_reports import _cell, _code

_T = TypeVar("_T")

_REASONING_CHARS = 300
_STATUS_DETAIL_CHARS = 120


class ReportFailed(click.ClickException):
    """Something the report names could not be read, so no report is written (exit 1)."""


class EnvRun(NamedTuple):
    environment: str
    run_id: str


class TraceEvaluationReport(NamedTuple):
    evaluation: TraceEvaluation
    # None until the trace evaluation has succeeded.
    result: Optional[TraceEvaluationResultsResponse]


class EnvRunReport(NamedTuple):
    environment: str
    run: RunResult


class Report(NamedTuple):
    generated_at: datetime
    evaluations: List[Evaluation]
    trace_evaluations: List[TraceEvaluationReport]
    environment_runs: List[EnvRunReport]
    # Whether --latest asked for the project's evaluations, so an empty list is news.
    latest: bool


@click.group(cls=LayerLensGroup)
def ci() -> None:
    """CI/CD pipeline helpers.

    \b
    Examples:
      layerlens ci report --evaluation-id ID -o "$GITHUB_STEP_SUMMARY"
      layerlens ci report --env-run support-desk:RUN_ID --format json
    """


def _ids(ctx: click.Context, param: click.Parameter, values: Tuple[str, ...]) -> Tuple[str, ...]:  # noqa: ARG001
    """The ids given, each once and in order; an empty one is refused, never sent."""
    ids = [value.strip() for value in values]
    if any(not id for id in ids):
        # Typically a CI step that failed before it wrote the id to the variable passed here.
        raise click.BadParameter("an id is empty")
    return tuple(dict.fromkeys(ids))


def _env_runs(ctx: click.Context, param: click.Parameter, values: Tuple[str, ...]) -> List[EnvRun]:  # noqa: ARG001
    runs: List[EnvRun] = []
    for value in values:
        # Split on the last colon: a run id never holds one.
        environment, colon, run_id = value.strip().rpartition(":")
        if not colon or not environment or not run_id:
            raise click.BadParameter(
                f"expected ENV:RUN_ID, the environment's name or id and the run's id (got {value!r})"
            )
        runs.append(EnvRun(environment, run_id))
    return list(dict.fromkeys(runs))


@ci.command("report")
@click.option(
    "--evaluation-id",
    "evaluation_ids",
    multiple=True,
    metavar="ID",
    callback=_ids,
    help="A benchmark evaluation to report, as `evaluate run` printed it (repeatable).",
)
@click.option(
    "--trace-evaluation-id",
    "trace_evaluation_ids",
    multiple=True,
    metavar="ID",
    callback=_ids,
    help="A trace evaluation to report, as `judge test` printed it (repeatable).",
)
@click.option(
    "--env-run",
    "env_runs",
    multiple=True,
    metavar="ENV:RUN_ID",
    callback=_env_runs,
    help="An environment run to report: the environment's name or id, a colon, the run's id (repeatable).",
)
@click.option(
    "--latest",
    default=None,
    type=click.IntRange(1, 500),
    metavar="N",
    help="Also report the project's N most recent benchmark evaluations, whoever started them.",
)
@click.option("--limit", default=None, type=click.IntRange(1, 500), hidden=True)
@click.option(
    "--output",
    "-o",
    "output_file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Append the Markdown report to this file (works with $GITHUB_STEP_SUMMARY); "
    "with --format json, write the JSON report to it.",
)
@click.option("--dry-run", is_flag=True, default=False, help="Say what would be reported, without fetching it.")
@click.pass_context
@handle_errors
def ci_report(
    ctx: click.Context,
    evaluation_ids: Tuple[str, ...],
    trace_evaluation_ids: Tuple[str, ...],
    env_runs: List[EnvRun],
    latest: Optional[int],
    limit: Optional[int],
    output_file: Optional[str],
    dry_run: bool,
) -> None:
    """Summarise the evaluations and runs a CI step names, for its job page.

    Reports what it is given: benchmark evaluations, trace evaluations and environment
    runs, by id. If one of them cannot be read, it exits 1 and writes no report. It
    reports results and does not judge them: it exits 0 whatever they are, so gate the
    build with the commands that ran them.

    \b
    Examples:
      layerlens ci report --evaluation-id ID -o "$GITHUB_STEP_SUMMARY"
      layerlens ci report --trace-evaluation-id ID --trace-evaluation-id ID2
      layerlens ci report --env-run support-desk:RUN_ID --format json -o report.json
      layerlens ci report --latest 10
    """
    if limit is not None:
        click.echo("warning: --limit is deprecated; use --latest N", err=True)
        latest = latest or limit
    if not (evaluation_ids or trace_evaluation_ids or env_runs or latest):
        raise click.UsageError(
            "name what to report with --evaluation-id, --trace-evaluation-id or --env-run ENV:RUN_ID "
            "(each repeatable), or pass --latest N for the project's N most recent benchmark evaluations",
            ctx,
        )

    if dry_run:
        echo_status(ctx, "[dry-run] Would report " + _what(evaluation_ids, trace_evaluation_ids, env_runs, latest))
        return

    report = _fetch(get_client(ctx), evaluation_ids, trace_evaluation_ids, env_runs, latest)

    if ctx.obj["output_format"] == "json":
        content = json.dumps(_as_json(report), indent=2) + "\n"
        mode = "w"
    else:
        content = _as_markdown(report)
        mode = "a"

    if output_file:
        with open(output_file, mode, encoding="utf-8") as f:
            f.write(content)
        echo_status(ctx, f"Report {'written' if mode == 'w' else 'appended'} to {output_file}")
    else:
        click.echo(content, nl=False)


def _what(
    evaluation_ids: Tuple[str, ...],
    trace_evaluation_ids: Tuple[str, ...],
    env_runs: List[EnvRun],
    latest: Optional[int],
) -> str:
    parts = [
        _count(len(evaluation_ids), "benchmark evaluation"),
        _count(len(trace_evaluation_ids), "trace evaluation"),
        _count(len(env_runs), "environment run"),
    ]
    what = [p for p in parts if p]
    if latest:
        what.append(f"the project's {latest} most recent benchmark evaluation(s)")
    return ", ".join(what)


def _count(n: int, noun: str) -> str:
    if not n:
        return ""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _read(what: str, fetch: Callable[[], Optional[_T]], *, none: str = ": the API's reply could not be read") -> _T:
    """``fetch()``, or :class:`ReportFailed` naming ``what`` when it fails, or ``what + none`` when it reads nothing."""
    try:
        value = fetch()
    except AuthenticationError:
        raise
    except APIStatusError as err:
        raise ReportFailed(f"{what}: {api_error_text(err)}") from None
    except LayerLensError as err:
        raise ReportFailed(f"{what}: {err}") from None
    if value is None:
        raise ReportFailed(what + none)
    return value


def _fetch(
    client: LayerLens,
    evaluation_ids: Tuple[str, ...],
    trace_evaluation_ids: Tuple[str, ...],
    env_runs: List[EnvRun],
    latest: Optional[int],
) -> Report:
    evaluations = [_read(f"evaluation {id}", partial(client.evaluations.get_by_id, id)) for id in evaluation_ids]
    if latest:
        page = _read(
            "the project's latest evaluations",
            lambda: client.evaluations.get_many(page_size=latest, sort_by="submitted_at", order="desc"),
        )
        named = {e.id for e in evaluations}
        evaluations.extend(e for e in page.evaluations if e.id not in named)

    trace_evaluations = [_trace_evaluation(client, id) for id in trace_evaluation_ids]

    environment_runs = [
        EnvRunReport(
            r.environment,
            _read(f"environment run {r.environment}:{r.run_id}", partial(_get_run, client, r)),
        )
        for r in env_runs
    ]
    return Report(datetime.now(timezone.utc), evaluations, trace_evaluations, environment_runs, latest=bool(latest))


def _get_run(client: LayerLens, run: EnvRun) -> RunResult:
    # Inside _read, so that client.environments refusing a login session names the run.
    return client.environments.runs.get(run.environment, run.run_id)


def _trace_evaluation(client: LayerLens, id: str) -> TraceEvaluationReport:
    what = f"trace evaluation {id}"
    te = _read(what, lambda: client.trace_evaluations.get(id))
    if _value(te.status) != "success":
        return TraceEvaluationReport(te, None)
    # get_results reads a 404 as None too, so None is a result that is missing or unreadable.
    result = _read(
        f"{what} succeeded, but its result",
        lambda: client.trace_evaluations.get_results(id),
        none=" is missing or could not be read",
    )
    return TraceEvaluationReport(te, result)


def _value(status: Any) -> str:
    return str(getattr(status, "value", status))


def _as_json(report: Report) -> Dict[str, Any]:
    return {
        "generated_at": report.generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "evaluations": [e.model_dump(mode="json") for e in report.evaluations],
        "trace_evaluations": [
            {
                **t.evaluation.model_dump(mode="json"),
                "result": None if t.result is None else t.result.model_dump(mode="json"),
            }
            for t in report.trace_evaluations
        ],
        "environment_runs": [
            {"environment": r.environment, **r.run.model_dump(mode="json")} for r in report.environment_runs
        ],
    }


def _as_markdown(report: Report) -> str:
    lines = ["## LayerLens CI report", ""]
    if report.evaluations or report.latest:
        lines.extend(_benchmark_section(report.evaluations))
    if report.trace_evaluations:
        lines.extend(_trace_section(report.trace_evaluations))
    for r in report.environment_runs:
        title = f"Environment {r.environment}: {r.run.agent_name or r.run.model or 'run'}"
        lines.extend([r.run.to_markdown(title=title).rstrip("\n"), ""])
    lines.append(f"*Generated by `layerlens ci report`, {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}*")
    return "\n".join(lines) + "\n\n"


def _benchmark_section(evaluations: List[Evaluation]) -> List[str]:
    lines = ["### Benchmark evaluations", ""]
    if not evaluations:
        return [*lines, "No benchmark evaluations in this project.", ""]
    lines.extend(
        [
            "| Evaluation | Model | Benchmark | Status | Accuracy | Failed prompts |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for e in evaluations:
        status = _status(_value(e.status), e.status_description)
        # accuracy is a percentage (0-100). The API updates it, and the failed prompts, as results
        # arrive: until the evaluation succeeds they cover only the prompts graded so far.
        succeeded = e.status == EvaluationStatus.SUCCESS
        accuracy = f"{e.accuracy:.1f}%" if succeeded else "-"
        failed = str(e.failed_prompt_count) if succeeded else "-"
        lines.append(
            f"| `{_code(e.id, table=True)}` | {_cell(e.model_name)} | {_cell(e.benchmark_name)} | "
            f"{status} | {accuracy} | {failed} |"
        )
    return [*lines, ""]


def _status(status: str, description: Optional[str]) -> str:
    """A status cell: the status (one of the API's own words), then why, if it is not ``success``."""
    if description and status != "success":
        return f"{status}: {_cell(description, _STATUS_DETAIL_CHARS)}"
    return status


def _trace_section(trace_evaluations: List[TraceEvaluationReport]) -> List[str]:
    lines = [
        "### Trace evaluations",
        "",
        "| Trace evaluation | Judge | Trace | Status | Verdict | Score |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    not_passed: List[str] = []
    for te, result in trace_evaluations:
        snapshot = te.judge_snapshot
        judge = _cell(f"{snapshot.name} v{snapshot.version}") if snapshot else f"`{_code(te.judge_id, table=True)}`"
        verdict = "-" if result is None else ("passed" if result.passed else "failed")
        score = "-" if result is None else f"{result.score:.2f}"
        status = _status(_value(te.status), te.status_description)
        lines.append(
            f"| `{_code(te.id, table=True)}` | {judge} | `{_code(te.trace_id, table=True)}` | {status} | "
            f"{verdict} | {score} |"
        )
        if result is not None and not result.passed:
            not_passed.append(f"- `{_code(te.id)}`: {_cell(result.reasoning, _REASONING_CHARS)}")
    lines.append("")
    if not_passed:
        lines.extend(["**Not passed**", "", *not_passed, ""])
    return lines
