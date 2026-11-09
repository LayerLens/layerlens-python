from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Tuple, Optional, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait as wait_for_futures

import click

from ._exec import (
    SUBMIT_GRACE_SECONDS,
    LaunchError,
    LiveProcesses,
    AttemptOutcome,
    run_attempt,
    outcome_line,
    with_run_label,
    with_exit_budget,
    agent_environment,
)
from ._common import (
    DURATION,
    DEFAULT_TIMEOUT,
    EXIT_NOT_FINISHED,
    fail,
    gate,
    progress,
    emit_json,
    print_run,
    env_client,
    env_errors,
    lost_start,
    task_names,
    finish_with,
    json_output,
    explain_gate,
    task_options,
    write_reports,
    api_error_text,
    report_options,
    start_not_known,
    run_summary_line,
    check_task_choice,
    interrupted_start,
    idempotency_option,
    interrupts_held_off,
    min_pass_rate_option,
    sigterm_as_interrupt,
)
from ..._command import LayerLensGroup, LayerLensCommand
from ..._formatter import format_table
from ...._exceptions import ConflictError, APIStatusError, LayerLensError
from ....models.environment import RUN_TERMINAL_STATUSES, RunResult
from ....resources.environments import ExternalRun, Environments, ExternalAttempt
from ....resources.environments._shared import new_idempotency_key

MAX_PARALLEL = 25

# The end of the first failed attempt's standard error, in lines, that explains a run in which no attempt answered.
STDERR_TAIL_LINES = 10


class _NotSubmitted(Exception):
    """The API refused an attempt's answer: the run cannot be graded as the agent ran it."""


@click.group("run", cls=LayerLensGroup)
def run() -> None:
    """Run your agent or a model against a set of tasks, graded, and read the results.

    \b
    Examples:
      layerlens env run exec support-desk --tasks-file ci/tasks.jsonl --agent-name support-agent --min-pass-rate 0.9 -- python ci/agent.py
      layerlens env run model support-desk --set ci --model openai/gpt-5 --budget 5.00
      layerlens env run results support-desk RUN_ID --junit results.xml
    """


@run.command("exec", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@task_options
@click.option("--agent-name", required=True, help="How the agent is labelled in results and traces.")
@click.option(
    "--label",
    default=None,
    help="Free text stored with the run, for example a commit SHA. Tags its traces, and the agent's own.",
)
@click.option(
    "--parallel", default=1, type=click.IntRange(1, MAX_PARALLEL), help=f"Attempts at once, up to {MAX_PARALLEL}."
)
@click.option("--attempt-timeout", default="10m", type=DURATION, help="Per attempt. Default 10m; then `timeout`.")
@min_pass_rate_option
@report_options
@click.option("--timeout", default=DEFAULT_TIMEOUT, type=DURATION, help="Wait this long for grading. Default 30m.")
@click.argument("command", nargs=-1, required=True, metavar="-- CMD...")
@env_errors
def exec_command(
    ctx: click.Context,
    environment: str,
    tasks_file: Optional[str],
    set_name: Optional[str],
    tasks: Optional[str],
    agent_name: str,
    label: Optional[str],
    parallel: int,
    attempt_timeout: float,
    min_pass_rate: Optional[float],
    junit: Optional[str],
    markdown: Optional[str],
    timeout: float,
    command: Tuple[str, ...],
) -> None:
    """Run your own agent, once per task, graded.

    For each task CMD runs with LAYERLENS_ENV_BASE_URL, LAYERLENS_ATTEMPT_TOKEN,
    LAYERLENS_TASK_PROMPT (also on standard input), LAYERLENS_TASK_ID, LAYERLENS_TASK_NAME,
    LAYERLENS_ATTEMPT_ID, LAYERLENS_ANSWER_FILE and LAYERLENS_TRACE_ID_FILE set, and without
    the LayerLens API key variables. Its answer is LAYERLENS_ANSWER_FILE, or its standard
    output when that file is empty. A non-zero exit grades the attempt `error`. The trace id
    it leaves in LAYERLENS_TRACE_ID_FILE, however it ends, links its own trace to the
    attempt; the LayerLens SDK's tracing writes it there. With --label, the label goes first in
    LAYERLENS_TRACE_TAGS, so the SDK tags every trace the agent records with it, as LayerLens
    tags each attempt's own trace. Unless the step sets LAYERLENS_EXIT_TIMEOUT, it is set to
    30 seconds (half --attempt-timeout when that is shorter), so the SDK uploads the agent's
    traces before it exits.

    The API key cannot be given with --api-key here, since every user on the machine can
    read a command line: use LAYERLENS_API_KEY or --api-key-file.

    Runs on Linux and macOS (WSL included), not on Windows.

    \b
    Exit codes: 0 success, 1 error, 2 usage, 10 below --min-pass-rate, 11 the run did not
    finish, or every attempt ended in an error.

    \b
    Examples:
      layerlens env run exec support-desk --tasks-file ci/tasks.jsonl --agent-name support-agent --parallel 4 --min-pass-rate 0.9 --junit results.xml --markdown "$GITHUB_STEP_SUMMARY" -- python ci/agent.py
    """
    if sys.platform == "win32":
        raise click.UsageError(
            "env run exec does not run on Windows: Windows does not end what an agent leaves running when it "
            "exits, so a leftover process could go on using the attempt's token. Run it on Linux or macOS (WSL "
            "works), or drive the run yourself with `env run start`, `submit` and `finish`, or with "
            "client.environments.external_run in Python."
        )
    if ctx.obj.get("api_key") and ctx.obj.get("api_key_source") == "--api-key":
        raise click.UsageError(
            "env run exec does not take --api-key: every user on this machine can read a command line, the "
            "agent's included. Put the key in LAYERLENS_API_KEY, or in a file for --api-key-file (- reads it "
            "from standard input)."
        )
    check_task_choice(tasks_file, set_name)
    client = env_client(ctx, key_on_command_line=False)
    envs = client.environments
    external = envs.external_run(
        envs.resolve(environment),
        agent_name=agent_name,
        set_name=set_name,
        tasks_file=tasks_file,
        task_names=task_names(tasks),
        label=label,
    )
    # The label first: a label that holds the key is then dropped with every other variable that does.
    agent_env = agent_environment(with_run_label(dict(os.environ), label), api_key=client.api_key)
    agent_env = with_exit_budget(agent_env, attempt_timeout)
    live = LiveProcesses()
    outcomes: List[Tuple[ExternalAttempt, AttemptOutcome]] = []
    with sigterm_as_interrupt():
        try:
            with external:
                progress(f"Run {external.run_id}: {len(external.attempts)} attempts, {parallel} at a time")
                outcomes = _execute(external.attempts, command, parallel, attempt_timeout, agent_env, live)
        except KeyboardInterrupt:
            if not external.started:
                fail("interrupted while starting the run; if it started, it closes on its own within 24 hours",
                     EXIT_NOT_FINISHED)  # fmt: skip
            fail(_cancelled_on_leaving(envs, external, "interrupted"), EXIT_NOT_FINISHED)
        except (LaunchError, _NotSubmitted) as err:
            if external.finished_before_cancel:
                fail(f"{err}; the run had already finished, so there was nothing to cancel")
            if external.cancel_error is None:
                fail(f"{err}; the run was cancelled")
            fail(f"{err}; the run could not be cancelled ({external.cancel_error}), and closes on its own within "
                 "24 hours")  # fmt: skip
        except LayerLensError as err:
            if external.started or not start_not_known(err):
                raise
            why = api_error_text(err) if isinstance(err, APIStatusError) else str(err).rstrip(".")
            fail(f"the run could not be started: {why}. exec sends a new Idempotency-Key each time, so run the "
                 "job again; if the API started this run all the same, it closes on its own within 24 hours.")  # fmt: skip
        result = _wait(envs, external.environment_id, external.run_id, timeout, started_here=True)
    _conclude(ctx, result, min_pass_rate, junit, markdown, broken=_no_answer(outcomes))


def _execute(
    attempts: Sequence[ExternalAttempt],
    command: Sequence[str],
    parallel: int,
    attempt_timeout: float,
    agent_env: Dict[str, str],
    live: LiveProcesses,
) -> List[Tuple[ExternalAttempt, AttemptOutcome]]:
    """Run every attempt and submit what it did; returns each attempt's outcome, in the run's order."""

    def work(attempt: ExternalAttempt) -> AttemptOutcome:
        outcome = run_attempt(attempt, command, timeout_seconds=attempt_timeout, environment=agent_env, live=live)
        with live.submitting() as allowed:
            if not allowed:
                return outcome
            try:
                if outcome.outcome == "answered":
                    assert outcome.answer is not None
                    attempt.submit(outcome.answer, trace_id=outcome.trace_id)
                else:
                    attempt.submit_error(outcome.error, timeout=outcome.outcome == "timeout", trace_id=outcome.trace_id)
            except LayerLensError as err:
                why = api_error_text(err) if isinstance(err, APIStatusError) else str(err)
                raise _NotSubmitted(
                    f"the answer to task {attempt.task_name!r} ({attempt.task_id}) was refused: {why}"
                ) from err
        return outcome

    pool = ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="layerlens-exec")
    futures: Dict[Future[AttemptOutcome], ExternalAttempt] = {pool.submit(work, a): a for a in attempts}
    done = 0
    pending = set(futures)
    try:
        while pending:
            # A bounded wait, so Ctrl-C reaches this thread on every platform.
            finished, pending = wait_for_futures(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in finished:
                outcome = future.result()
                done += 1
                progress(outcome_line(done, len(attempts), futures[future], outcome))
    except BaseException:
        live.stop_all()
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False)
        _let_submits_end(live)
        raise
    pool.shutdown(wait=True)
    return [(attempt, future.result()) for future, attempt in futures.items()]


def _let_submits_end(live: LiveProcesses) -> None:
    """Once the run is stopping, give the answers already on their way time to be graded.

    The run is cancelled next. Waiting first means the API takes the run's submits, then its
    cancel, in that order; a submit still going after ``SUBMIT_GRACE_SECONDS`` meets a cancelled
    run and is refused. A second Ctrl-C cuts the wait short.
    """
    under_way = live.submits_under_way()
    if not under_way:
        return
    answers = "answer is" if under_way == 1 else "answers are"
    progress(
        f"{under_way} {answers} being submitted; waiting up to {SUBMIT_GRACE_SECONDS:g}s before cancelling the run"
    )
    live.wait_for_submits(SUBMIT_GRACE_SECONDS)


def _no_answer(outcomes: Sequence[Tuple[ExternalAttempt, AttemptOutcome]]) -> Optional[str]:
    """Why the run says nothing about the agent, when every attempt ended in an error.

    That is a broken command (a typo, a missing dependency) as much as an agent that crashes
    on every task; the two look the same from here. An attempt that answered, or ran out of
    time, is a verdict on the agent, and the run is gated as usual.
    """
    if not outcomes or any(outcome.outcome != "error" for _, outcome in outcomes):
        return None
    attempt, first = outcomes[0]
    words = (
        f"no attempt gave an answer, so this run says nothing about the agent: all {len(outcomes)} attempts ended "
        f"in an error. The first, task {attempt.task_name!r}, failed because {first.error}"
    )
    tail = first.stderr_tail.strip().splitlines()[-STDERR_TAIL_LINES:]
    if not tail:
        return words + ", and wrote nothing to standard error."
    return words + ". The end of its standard error:\n" + "\n".join(f"  {line}" for line in tail)


@run.command("model", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@task_options
@click.option("--model", default=None, help="A catalog model, or custom:<key> for one of your project's models.")
@click.option("--endpoint", default=None, help="An OpenAI-compatible chat-completions endpoint (HTTPS) instead.")
@click.option("--endpoint-model", default=None, help="The model the endpoint serves.")
@click.option("--endpoint-key-env", default=None, metavar="VAR", help="The variable holding the endpoint's key.")
@click.option(
    "--budget", required=True, type=click.FloatRange(min=0, min_open=True, max=100), help="Required, up to 100 USD."
)
@click.option("--system-prompt-file", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option("--max-steps", default=None, type=click.IntRange(1, 50), help="Tool-calling steps per attempt.")
@click.option("--agent-name", default=None, help="How the agent is labelled in results and traces.")
@click.option("--label", default=None, help="Free text stored with the run, for example a commit SHA.")
@min_pass_rate_option
@report_options
@click.option("--no-wait", is_flag=True, default=False, help="Print the run id and return at once.")
@click.option(
    "--timeout", default=DEFAULT_TIMEOUT, type=DURATION, help="Wait this long for the run, then cancel it. Default 30m."
)
@idempotency_option("run")
@env_errors
def model_command(
    ctx: click.Context,
    environment: str,
    tasks_file: Optional[str],
    set_name: Optional[str],
    tasks: Optional[str],
    model: Optional[str],
    endpoint: Optional[str],
    endpoint_model: Optional[str],
    endpoint_key_env: Optional[str],
    budget: float,
    system_prompt_file: Optional[str],
    max_steps: Optional[int],
    agent_name: Optional[str],
    label: Optional[str],
    min_pass_rate: Optional[float],
    junit: Optional[str],
    markdown: Optional[str],
    no_wait: bool,
    timeout: float,
    idempotency_key: Optional[str],
) -> None:
    """Run a model, or an agent behind an OpenAI-compatible endpoint, through LayerLens's own loop.

    A run still going at --timeout is cancelled, and the command exits 11.

    \b
    Examples:
      layerlens env run model support-desk --set ci --model openai/gpt-5 --budget 5.00 --min-pass-rate 0.8
      layerlens env run model support-desk --set ci --endpoint https://agents.example.com/v1 --endpoint-model support-agent-v7 --endpoint-key-env SUPPORT_AGENT_KEY --budget 5.00
    """
    check_task_choice(tasks_file, set_name)
    endpoint_body = _endpoint(model, endpoint, endpoint_model, endpoint_key_env)
    system_prompt = None
    if system_prompt_file is not None:
        with open(system_prompt_file, encoding="utf-8") as f:
            system_prompt = f.read()
    envs = env_client(ctx).environments
    environment_id = envs.resolve(environment)
    key = idempotency_key or new_idempotency_key()
    try:
        with sigterm_as_interrupt():
            started = envs.runs.run_model(
                environment_id,
                budget_usd=budget,
                model=model,
                endpoint=endpoint_body,
                set_name=set_name,
                tasks_file=tasks_file,
                task_names=task_names(tasks),
                agent_name=agent_name,
                label=label,
                system_prompt=system_prompt,
                max_steps=max_steps,
                idempotency_key=key,
            )
    except KeyboardInterrupt:
        fail(interrupted_start("run", key), EXIT_NOT_FINISHED)
    except LayerLensError as err:
        if not start_not_known(err):
            raise
        fail(lost_start(err, "run", key))
    progress(f"Run {started.run_id}: {started.total} tasks with {started.model}")
    if no_wait:
        if json_output(ctx):
            emit_json(started)
        else:
            click.echo(started.run_id)
        return
    result = _wait(envs, environment_id, started.run_id, timeout, started_here=True)
    _conclude(ctx, result, min_pass_rate, junit, markdown)


def _endpoint(
    model: Optional[str], endpoint: Optional[str], endpoint_model: Optional[str], endpoint_key_env: Optional[str]
) -> Optional[Dict[str, str]]:
    if endpoint is None:
        if model is None:
            raise click.UsageError("pass --model, or --endpoint with --endpoint-model")
        if endpoint_model is not None or endpoint_key_env is not None:
            raise click.UsageError("--endpoint-model and --endpoint-key-env go with --endpoint")
        return None
    if model is not None:
        raise click.UsageError("pass --model or --endpoint, not both")
    if endpoint_model is None:
        raise click.UsageError("--endpoint needs --endpoint-model, the model it serves")
    body = {"base_url": endpoint, "model": endpoint_model}
    if endpoint_key_env is not None:
        key = os.environ.get(endpoint_key_env)
        if not key:
            raise click.UsageError(f"--endpoint-key-env names {endpoint_key_env}, which is not set")
        body["api_key"] = key
    return body


def _cancel(envs: Environments, environment_id: str, run_id: str, why: str) -> Tuple[str, bool]:
    """Cancel a run, and say why and what became of it; and whether it had finished before the cancel."""
    with interrupts_held_off():
        try:
            status = envs.runs.cancel(environment_id, run_id).status
        except ConflictError:
            return _finished_by_then(run_id, why), True
        except LayerLensError as err:
            return f"{why}, and {_the_run(run_id, why)} could not be cancelled: {err}", False
    return _cancelled_as(run_id, why, status), False


def _cancelled_on_leaving(envs: Environments, external: ExternalRun, why: str) -> str:
    """What became of the run that leaving ``with external`` on an interrupt cancelled.

    That cancel is the run's one cancel (BUG-09: exec used to send a second, and a run that had
    finished meanwhile printed the SDK's raw 409 before exec's own line). exec cancels again only
    when that cancel did not happen at all: a second Ctrl-C cut it short.
    """
    if external.finished_before_cancel:
        return _finished_by_then(external.run_id, why)
    if external.cancelled_run is not None:
        return _cancelled_as(external.run_id, why, external.cancelled_run.status)
    if external.cancel_error is not None:
        return f"{why}, and {_the_run(external.run_id, why)} could not be cancelled: {external.cancel_error}"
    return _cancel(envs, external.environment_id, external.run_id, why)[0]


def _the_run(run_id: str, why: str) -> str:
    return "it" if run_id in why else f"run {run_id}"


def _finished_by_then(run_id: str, why: str) -> str:
    return f"{why}, and {_the_run(run_id, why)} had finished by then; read it with `layerlens env run results`"


def _cancelled_as(run_id: str, why: str, status: str) -> str:
    if status in RUN_TERMINAL_STATUSES:
        return f"{why}, so {_the_run(run_id, why)} was {status}"
    return f"{why}, so {_the_run(run_id, why)} is cancelling, and stops once the tasks in flight finish"


def _finished_meanwhile(envs: Environments, environment_id: str, run_id: str) -> Optional[RunResult]:
    """The run, if it finished between the last poll and the cancel."""
    try:
        run = envs.runs.get(environment_id, run_id)
    except LayerLensError:
        return None
    return run if run.finished else None


def _wait(
    envs: Environments,
    environment_id: str,
    run_id: str,
    timeout: float,
    *,
    started_here: bool,
    cancel_on_timeout: bool = False,
) -> RunResult:
    """Wait for the run to finish. An interrupt, or a timeout, exits 11; either cancels a run this command started.

    A run that finished just as the timeout passed, so that the cancel finds it ended, is returned to be gated.
    """
    with sigterm_as_interrupt():
        try:
            return envs.runs.wait(environment_id, run_id, timeout_seconds=timeout)
        except KeyboardInterrupt:
            if started_here:
                fail(_cancel(envs, environment_id, run_id, "interrupted")[0], EXIT_NOT_FINISHED)
            fail(f"interrupted while waiting; run {run_id} goes on: read it with `layerlens env run results`",
                 EXIT_NOT_FINISHED)  # fmt: skip
        except TimeoutError as err:
            if not (started_here or cancel_on_timeout):
                raise
            said, ended = _cancel(envs, environment_id, run_id, str(err))
            finished = _finished_meanwhile(envs, environment_id, run_id) if ended else None
            if finished is None:
                fail(said, EXIT_NOT_FINISHED)
            progress(f"Run {run_id} finished as --timeout passed, so it is gated as it ended")
            return finished


def _conclude(
    ctx: click.Context,
    result: RunResult,
    min_pass_rate: Optional[float],
    junit: Optional[str],
    markdown: Optional[str],
    *,
    broken: Optional[str] = None,
) -> None:
    print_run(ctx, result)
    write_reports(result, junit, markdown)
    if broken is not None:
        fail(broken, EXIT_NOT_FINISHED)
    code = gate(result, min_pass_rate)
    explain_gate(result, min_pass_rate, code)
    finish_with(code)


@run.command("start", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@task_options
@click.option("--agent-name", required=True, help="How the agent is labelled in results and traces.")
@click.option("--label", default=None, help="Free text stored with the run, for example a commit SHA.")
@idempotency_option("run")
@env_errors
def start(
    ctx: click.Context,
    environment: str,
    tasks_file: Optional[str],
    set_name: Optional[str],
    tasks: Optional[str],
    agent_name: str,
    label: Optional[str],
    idempotency_key: Optional[str],
) -> None:
    """Start a run of your own agent and print its attempts, to drive it step by step.

    Each attempt has a base_url and a token; --format json prints them all.

    \b
    Examples:
      layerlens --format json env run start support-desk --tasks-file ci/tasks.jsonl --agent-name support-agent > run.json
    """
    check_task_choice(tasks_file, set_name)
    envs = env_client(ctx).environments
    environment_id = envs.resolve(environment)
    key = idempotency_key or new_idempotency_key()
    try:
        with sigterm_as_interrupt():
            started = envs.runs.start_external(
                environment_id,
                agent_name=agent_name,
                set_name=set_name,
                tasks_file=tasks_file,
                task_names=task_names(tasks),
                label=label,
                idempotency_key=key,
            )
    except KeyboardInterrupt:
        fail(interrupted_start("run", key), EXIT_NOT_FINISHED)
    except LayerLensError as err:
        if not start_not_known(err):
            raise
        fail(lost_start(err, "run", key))
    if json_output(ctx):
        emit_json(started)
        return
    click.echo(f"run {started.run_id}")
    rows: List[Dict[str, Any]] = [
        {"attempt_id": a.attempt_id, "task": a.task_name, "expires_at": a.expires_at.isoformat()}
        for a in started.attempts
    ]
    click.echo(format_table(rows, [("attempt_id", "Attempt"), ("task", "Task"), ("expires_at", "Expires")]))
    progress("Run with --format json for each attempt's base_url and token.")


@run.command("submit", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.argument("run_id")
@click.option("--attempt", "attempt_id", required=True, help="The attempt's id.")
@click.option("--answer", default=None, help="The agent's final answer.")
@click.option(
    "--answer-file", default=None, type=click.Path(exists=True, dir_okay=False), help="Read the answer from a file."
)
@click.option("--error", "error", default=None, help="The agent gave no answer: why. Graded `error`.")
@click.option("--timed-out", is_flag=True, default=False, help="The agent ran out of time. Graded `timeout`.")
@click.option("--trace-id", default=None, help="The trace the agent sent, to link it to the attempt.")
@env_errors
def submit(
    ctx: click.Context,
    environment: str,
    run_id: str,
    attempt_id: str,
    answer: Optional[str],
    answer_file: Optional[str],
    error: Optional[str],
    timed_out: bool,
    trace_id: Optional[str],
) -> None:
    """Submit one attempt's answer; it is graded before this returns.

    \b
    Examples:
      layerlens env run submit support-desk RUN_ID --attempt ATTEMPT_ID --answer-file answer.txt
      layerlens env run submit support-desk RUN_ID --attempt ATTEMPT_ID --timed-out
    """
    if answer is not None and answer_file is not None:
        raise click.UsageError("pass --answer or --answer-file, not both")
    if answer_file is not None:
        with open(answer_file, encoding="utf-8") as f:
            answer = f.read()
    if answer is not None and (error is not None or timed_out):
        raise click.UsageError("an answer goes alone; --error and --timed-out are for an agent that gave none")
    if answer is None and error is None and not timed_out:
        raise click.UsageError("pass --answer, --answer-file, --error or --timed-out")
    outcome = "answered" if answer is not None else ("timeout" if timed_out else "error")
    result = env_client(ctx).environments.runs.submit(
        environment, run_id, attempt_id, answer, outcome=outcome, error=error, trace_id=trace_id
    )
    if result.trace_id_unlinked:  # graded, but the id names no trace the API could link (N3)
        progress(f"Warning: {result.trace_id_unlinked}")
    if json_output(ctx):
        emit_json(result)
    else:
        done = " The run is finished." if result.finished else ""
        click.echo(f"Graded attempt {result.attempt_id}: {result.answered} of {result.total} answered.{done}")


@run.command("finish", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.argument("run_id")
@env_errors
def finish(ctx: click.Context, environment: str, run_id: str) -> None:
    """Close a run of your own agent; attempts never submitted count as errors."""
    print_run(ctx, env_client(ctx).environments.runs.finish(environment, run_id))


@run.command("status", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.argument("run_id")
@env_errors
def status(ctx: click.Context, environment: str, run_id: str) -> None:
    """One line on a run: its status and counts."""
    result = env_client(ctx).environments.runs.get(environment, run_id)
    if json_output(ctx):
        emit_json(result)
    else:
        click.echo(run_summary_line(result))


@run.command("wait", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.argument("run_id")
@min_pass_rate_option
@report_options
@click.option("--timeout", default=DEFAULT_TIMEOUT, type=DURATION, help="Default 30m; then exit 11.")
@click.option(
    "--cancel-on-timeout", is_flag=True, default=False, help="Cancel the run if it is still going at --timeout."
)
@env_errors
def wait(
    ctx: click.Context,
    environment: str,
    run_id: str,
    min_pass_rate: Optional[float],
    junit: Optional[str],
    markdown: Optional[str],
    timeout: float,
    cancel_on_timeout: bool,
) -> None:
    """Block until a run finishes, and gate on it.

    At --timeout the run goes on, unless --cancel-on-timeout; either way the command exits 11.

    \b
    Examples:
      layerlens env run wait support-desk RUN_ID --min-pass-rate 0.9 --timeout 30m
    """
    envs = env_client(ctx).environments
    environment_id = envs.resolve(environment)
    result = _wait(envs, environment_id, run_id, timeout, started_here=False, cancel_on_timeout=cancel_on_timeout)
    _conclude(ctx, result, min_pass_rate, junit, markdown)


@run.command("results", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.argument("run_id")
@report_options
@env_errors
def results(ctx: click.Context, environment: str, run_id: str, junit: Optional[str], markdown: Optional[str]) -> None:
    """A run's results, every attempt with its verdict and evidence.

    \b
    Examples:
      layerlens env run results support-desk RUN_ID --junit results.xml --markdown summary.md
      layerlens --format json env run results support-desk RUN_ID
    """
    result = env_client(ctx).environments.runs.get(environment, run_id)
    if json_output(ctx):
        emit_json(result)
    else:
        click.echo(run_summary_line(result))
        rows = [
            {
                "task": a.task_name or a.task_id,
                "verdict": a.verdict or "pending",
                "evidence": " ".join((a.evidence or "").split()),
                "trace_id": a.trace_id,
            }
            for a in result.attempts
        ]
        columns = [("task", "Task"), ("verdict", "Verdict"), ("evidence", "Evidence"), ("trace_id", "Trace")]
        click.echo(format_table(rows, columns, max_col_width=80))
    write_reports(result, junit, markdown)


@run.command("cancel", cls=LayerLensCommand)
@click.argument("environment", metavar="ENV")
@click.argument("run_id")
@env_errors
def cancel(ctx: click.Context, environment: str, run_id: str) -> None:
    """Cancel a run."""
    print_run(ctx, env_client(ctx).environments.runs.cancel(environment, run_id))
