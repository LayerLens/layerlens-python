from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Union, Mapping, Optional, Sequence
from pathlib import Path

from ._shared import (
    DEFAULT_POLL_SECONDS,
    DEFAULT_WAIT_SECONDS,
    TaskInput,
    SubmitState,
    SubmitPolicy,
    IdempotencyKey,
    InvalidArgument,
    still,
    submit_body,
    task_choice,
    wait_policy,
    submit_state,
    new_idempotency_key,
)
from ..._polling import POLL_RETRIES, is_transient
from ..._exceptions import LayerLensError
from ...models.environment import RunResult, SubmitResult, ExternalRunStart

if TYPE_CHECKING:
    from .environments import Environments, AsyncEnvironments


class Runs:
    """``client.environments.runs``: graded runs of a model, an endpoint or your own agent."""

    def __init__(self, environments: Environments) -> None:
        self._environments = environments

    def run_model(
        self,
        environment: str,
        *,
        budget_usd: float,
        model: Optional[str] = None,
        endpoint: Optional[Mapping[str, str]] = None,
        set_name: Optional[str] = None,
        tasks: Optional[Sequence[TaskInput]] = None,
        tasks_file: Union[str, Path, None] = None,
        task_names: Optional[Sequence[str]] = None,
        agent_name: Optional[str] = None,
        label: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_steps: Optional[int] = None,
        idempotency_key: IdempotencyKey = None,
    ) -> RunResult:
        """Start a run of LayerLens's own agent loop with ``model``, once per task.

        ``model`` is a catalog model or ``custom:<key>``; ``endpoint`` instead is
        ``{"base_url", "model", "api_key"}`` for an OpenAI-compatible endpoint. Inline
        ``tasks`` (or ``tasks_file``) take up to 25 tasks. The run stops at ``budget_usd``.
        Returns the run as started; follow it with :meth:`wait`.
        """
        envs = self._environments
        body = _rollout_body(
            budget_usd, model, endpoint, set_name, tasks, tasks_file, task_names, agent_name, label,
            system_prompt, max_steps,
        )  # fmt: skip
        return envs._call(
            "POST", envs._path(envs.resolve(environment), "rollout-runs"), RunResult, body=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )  # fmt: skip

    def get(self, environment: str, run_id: str) -> RunResult:
        return self._read(self._environments.resolve(environment), run_id)

    def _read(self, environment_id: str, run_id: str, **request: Any) -> RunResult:
        envs = self._environments
        return envs._call("GET", envs._path(environment_id, "runs", run_id), RunResult, **request)

    def wait(
        self,
        environment: str,
        run_id: str,
        *,
        timeout_seconds: Optional[float] = DEFAULT_WAIT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> RunResult:
        """Block until the run finishes, and return it, whatever its ``status``.

        A run of your own agent finishes when its last attempt is graded, or on ``finish``.
        A poll that fails on a 429, a 5xx or a lost connection does not end the wait.
        Raises :class:`TimeoutError` when the run is still going after ``timeout_seconds``,
        or the polls kept failing until then.
        """
        environment_id = self._environments.resolve(environment)
        policy = wait_policy(f"run {run_id}", timeout_seconds, poll_interval_seconds)
        while True:
            try:
                run = self._read(environment_id, run_id, max_retries=POLL_RETRIES)
            except LayerLensError as err:
                delay = policy.after_error(err)
            else:
                if run.finished:
                    return run
                delay = policy.after_pending(still(f"run {run_id} is still {run.status}", timeout_seconds))
            self._environments._sleep(delay)

    def cancel(self, environment: str, run_id: str) -> RunResult:
        """Cancel a run. A model run stops after the tasks in flight; a run of your own agent ends at once."""
        envs = self._environments
        return envs._call("POST", envs._path(envs.resolve(environment), "runs", run_id, "cancel"), RunResult)

    def start_external(
        self,
        environment: str,
        *,
        agent_name: str,
        set_name: Optional[str] = None,
        tasks: Optional[Sequence[TaskInput]] = None,
        tasks_file: Union[str, Path, None] = None,
        task_names: Optional[Sequence[str]] = None,
        label: Optional[str] = None,
        idempotency_key: IdempotencyKey = None,
    ) -> ExternalRunStart:
        """Start a run of your own agent: one attempt per task (up to 300), each with its own address and token.

        :meth:`Environments.external_run` wraps this, :meth:`submit` and :meth:`finish`.
        """
        body = external_run_body(agent_name, set_name, tasks, tasks_file, task_names, label)
        return self._start_external(environment, body, idempotency_key or new_idempotency_key())

    def _start_external(self, environment: str, body: Dict[str, Any], idempotency_key: str) -> ExternalRunStart:
        envs = self._environments
        return envs._call(
            "POST", envs._path(envs.resolve(environment), "external-runs"), ExternalRunStart, body=body,
            idempotency_key=idempotency_key,
        )  # fmt: skip

    def submit(
        self,
        environment: str,
        run_id: str,
        attempt_id: str,
        answer: Optional[str] = None,
        *,
        outcome: str = "answered",
        error: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> SubmitResult:
        """Submit an attempt's final answer; it is graded before this returns.

        ``outcome`` is ``"error"`` or ``"timeout"`` for an agent that gave no answer,
        with ``error`` saying why. A submit that times out or fails on the server's side
        may still have been graded, and one the API holds back (a call of the attempt still
        being served) may go through shortly: both are retried and settled from the run,
        within about 7 minutes. Submitting an attempt already graded raises
        :class:`ConflictError`, as does one for a run that has ended.
        """
        envs = self._environments
        environment_id = envs.resolve(environment)
        path = envs._path(environment_id, "external-runs", run_id, "attempts", attempt_id, "submit")
        body = submit_body(answer=answer, outcome=outcome, error=error, trace_id=trace_id)
        policy = SubmitPolicy(run_id, attempt_id)
        send = True
        while True:
            if send:
                try:
                    return envs._call("POST", path, SubmitResult, body=body, max_retries=0)
                except LayerLensError as err:
                    wait = policy.after_error(err)
                if wait is not None:
                    envs._sleep(wait)
                    continue
            try:
                state: Optional[SubmitState] = submit_state(self.get(environment_id, run_id), attempt_id)
            except LayerLensError as err:
                if not is_transient(err):
                    raise
                state = None
            result = policy.after_state(state)
            if result is not None:
                return result
            envs._sleep(policy.pause())
            send = policy.send_again

    def finish(self, environment: str, run_id: str) -> RunResult:
        """Close a run of your own agent early; attempts never submitted count as ``error``."""
        envs = self._environments
        return envs._call("POST", envs._path(envs.resolve(environment), "external-runs", run_id, "finish"), RunResult)


class AsyncRuns:
    """``client.environments.runs`` on :class:`AsyncLayerLens`. See :class:`Runs`."""

    def __init__(self, environments: AsyncEnvironments) -> None:
        self._environments = environments

    async def run_model(
        self,
        environment: str,
        *,
        budget_usd: float,
        model: Optional[str] = None,
        endpoint: Optional[Mapping[str, str]] = None,
        set_name: Optional[str] = None,
        tasks: Optional[Sequence[TaskInput]] = None,
        tasks_file: Union[str, Path, None] = None,
        task_names: Optional[Sequence[str]] = None,
        agent_name: Optional[str] = None,
        label: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_steps: Optional[int] = None,
        idempotency_key: IdempotencyKey = None,
    ) -> RunResult:
        envs = self._environments
        body = _rollout_body(
            budget_usd, model, endpoint, set_name, tasks, tasks_file, task_names, agent_name, label,
            system_prompt, max_steps,
        )  # fmt: skip
        return await envs._call(
            "POST", envs._path(await envs.resolve(environment), "rollout-runs"), RunResult, body=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )  # fmt: skip

    async def get(self, environment: str, run_id: str) -> RunResult:
        return await self._read(await self._environments.resolve(environment), run_id)

    async def _read(self, environment_id: str, run_id: str, **request: Any) -> RunResult:
        envs = self._environments
        return await envs._call("GET", envs._path(environment_id, "runs", run_id), RunResult, **request)

    async def wait(
        self,
        environment: str,
        run_id: str,
        *,
        timeout_seconds: Optional[float] = DEFAULT_WAIT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> RunResult:
        environment_id = await self._environments.resolve(environment)
        policy = wait_policy(f"run {run_id}", timeout_seconds, poll_interval_seconds)
        while True:
            try:
                run = await self._read(environment_id, run_id, max_retries=POLL_RETRIES)
            except LayerLensError as err:
                delay = policy.after_error(err)
            else:
                if run.finished:
                    return run
                delay = policy.after_pending(still(f"run {run_id} is still {run.status}", timeout_seconds))
            await self._environments._sleep(delay)

    async def cancel(self, environment: str, run_id: str) -> RunResult:
        envs = self._environments
        path = envs._path(await envs.resolve(environment), "runs", run_id, "cancel")
        return await envs._call("POST", path, RunResult)

    async def start_external(
        self,
        environment: str,
        *,
        agent_name: str,
        set_name: Optional[str] = None,
        tasks: Optional[Sequence[TaskInput]] = None,
        tasks_file: Union[str, Path, None] = None,
        task_names: Optional[Sequence[str]] = None,
        label: Optional[str] = None,
        idempotency_key: IdempotencyKey = None,
    ) -> ExternalRunStart:
        body = external_run_body(agent_name, set_name, tasks, tasks_file, task_names, label)
        return await self._start_external(environment, body, idempotency_key or new_idempotency_key())

    async def _start_external(self, environment: str, body: Dict[str, Any], idempotency_key: str) -> ExternalRunStart:
        envs = self._environments
        return await envs._call(
            "POST", envs._path(await envs.resolve(environment), "external-runs"), ExternalRunStart, body=body,
            idempotency_key=idempotency_key,
        )  # fmt: skip

    async def submit(
        self,
        environment: str,
        run_id: str,
        attempt_id: str,
        answer: Optional[str] = None,
        *,
        outcome: str = "answered",
        error: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> SubmitResult:
        envs = self._environments
        environment_id = await envs.resolve(environment)
        path = envs._path(environment_id, "external-runs", run_id, "attempts", attempt_id, "submit")
        body = submit_body(answer=answer, outcome=outcome, error=error, trace_id=trace_id)
        policy = SubmitPolicy(run_id, attempt_id)
        send = True
        while True:
            if send:
                try:
                    return await envs._call("POST", path, SubmitResult, body=body, max_retries=0)
                except LayerLensError as err:
                    wait = policy.after_error(err)
                if wait is not None:
                    await envs._sleep(wait)
                    continue
            try:
                state: Optional[SubmitState] = submit_state(await self.get(environment_id, run_id), attempt_id)
            except LayerLensError as err:
                if not is_transient(err):
                    raise
                state = None
            result = policy.after_state(state)
            if result is not None:
                return result
            await envs._sleep(policy.pause())
            send = policy.send_again

    async def finish(self, environment: str, run_id: str) -> RunResult:
        envs = self._environments
        path = envs._path(await envs.resolve(environment), "external-runs", run_id, "finish")
        return await envs._call("POST", path, RunResult)


def _rollout_body(
    budget_usd: float,
    model: Optional[str],
    endpoint: Optional[Mapping[str, str]],
    set_name: Optional[str],
    tasks: Optional[Sequence[TaskInput]],
    tasks_file: Union[str, Path, None],
    task_names: Optional[Sequence[str]],
    agent_name: Optional[str],
    label: Optional[str],
    system_prompt: Optional[str],
    max_steps: Optional[int],
) -> Dict[str, Any]:
    if model is None and endpoint is None:
        raise InvalidArgument("a model run needs model, or endpoint for an OpenAI-compatible endpoint")
    body = task_choice(set_name=set_name, tasks=tasks, tasks_file=tasks_file, task_names=task_names)
    body["budget_usd"] = budget_usd
    for key, value in (
        ("model", model),
        ("endpoint", None if endpoint is None else dict(endpoint)),
        ("agent_name", agent_name),
        ("label", label),
        ("system_prompt", system_prompt),
        ("max_steps", max_steps),
    ):
        if value is not None:
            body[key] = value
    return body


def external_run_body(
    agent_name: str,
    set_name: Optional[str],
    tasks: Optional[Sequence[TaskInput]],
    tasks_file: Union[str, Path, None],
    task_names: Optional[Sequence[str]],
    label: Optional[str],
) -> Dict[str, Any]:
    body = task_choice(set_name=set_name, tasks=tasks, tasks_file=tasks_file, task_names=task_names)
    body["agent_name"] = agent_name
    if label is not None:
        body["label"] = label
    return body
