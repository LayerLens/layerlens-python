from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Union, Mapping, Iterator, Optional, Sequence, AsyncIterator
from pathlib import Path

from ._shared import (
    SUBMIT_LESS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_WAIT_SECONDS,
    RUN_FEWER_INLINE_TASKS,
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
from ..._resource import AsyncOrganizationScoped
from ..._exceptions import LayerLensError
from ...models.environment import RunPage, RunResult, RunSummary, SubmitResult, ExternalRunStart

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
            idempotency_key=idempotency_key or new_idempotency_key(), remedy=RUN_FEWER_INLINE_TASKS,
        )  # fmt: skip

    def list(
        self,
        environment: str,
        *,
        label: Optional[str] = None,
        agent_name: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> RunPage:
        """One page of the environment's runs, newest first; :meth:`iter` reads every page.

        ``label`` and ``agent_name`` match exactly, as the run was started with them; ``kind``
        is ``"external"`` or ``"rollout"``; ``status`` is the status a run shows, as
        :meth:`get` returns it. ``limit`` is up to 50 (20 by default). Pass the page's
        ``next_cursor`` as ``cursor`` for the next one. A page filtered by ``status`` can
        hold fewer runs than ``limit``, or none, and still have a ``next_cursor``.

        The newest run on ``main`` that finished::

            page = client.environments.runs.list("support-desk", label="main", status="success", limit=1)
        """
        envs = self._environments
        params = runs_list_params(label, agent_name, kind, status, limit, cursor)
        return envs._call("GET", envs._path(envs.resolve(environment), "runs"), RunPage, params=params)

    def iter(
        self,
        environment: str,
        *,
        label: Optional[str] = None,
        agent_name: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        page_size: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Iterator[RunSummary]:
        """Every run :meth:`list` matches, newest first, reading a page at a time as you go."""
        environment_id = self._environments.resolve(environment)
        while True:
            page = self.list(
                environment_id, label=label, agent_name=agent_name, kind=kind, status=status, limit=page_size,
                cursor=cursor,
            )  # fmt: skip
            yield from page.runs
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

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

        Each request, the lookup of an environment by name included, is sent once with the client's
        timeout cut to the time left (httpx applies it to connecting and to each wait for data), and
        none is sent once ``timeout_seconds`` has passed. The wait aims a poll at 2 seconds before
        the deadline, so that poll has time to answer; polls can also run inside those 2 seconds,
        none with less than half a second left. A wait never polls sooner than a ``Retry-After``
        asks. When the ``Retry-After`` reaches past the wait's deadline, the wait sleeps until the
        deadline and then raises, with the 429 as the cause. A wait with no deadline
        (``timeout_seconds=None``) reads a ``Retry-After`` longer than 5 minutes as 5 minutes.
        ``timeout_seconds=0`` checks once. A client's first organization read, made once per client,
        comes before the wait and is not counted in ``timeout_seconds``.
        """
        # The client's first organization read, made once per client, comes before the deadline.
        self._environments._client._organization_scope()
        policy = wait_policy(f"run {run_id}", timeout_seconds, poll_interval_seconds)
        environment_id: Optional[str] = None
        while True:
            try:
                if environment_id is None:
                    environment_id = self._environments._resolve(environment, policy=policy)
                run = self._read(
                    environment_id,
                    run_id,
                    max_retries=POLL_RETRIES,
                    timeout=policy.poll_timeout(self._environments._client.timeout),
                )
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
            idempotency_key=idempotency_key, remedy=RUN_FEWER_INLINE_TASKS,
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
                    return envs._call("POST", path, SubmitResult, body=body, max_retries=0, remedy=SUBMIT_LESS)
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


class AsyncRuns(AsyncOrganizationScoped):
    """``client.environments.runs`` on :class:`AsyncLayerLens`. See :class:`Runs`."""

    def __init__(self, environments: AsyncEnvironments) -> None:
        self._environments = environments

    def _organization_client(self) -> Any:
        return self._environments._client

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
            idempotency_key=idempotency_key or new_idempotency_key(), remedy=RUN_FEWER_INLINE_TASKS,
        )  # fmt: skip

    async def list(
        self,
        environment: str,
        *,
        label: Optional[str] = None,
        agent_name: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> RunPage:
        envs = self._environments
        params = runs_list_params(label, agent_name, kind, status, limit, cursor)
        return await envs._call("GET", envs._path(await envs.resolve(environment), "runs"), RunPage, params=params)

    async def iter(
        self,
        environment: str,
        *,
        label: Optional[str] = None,
        agent_name: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        page_size: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> AsyncIterator[RunSummary]:
        environment_id = await self._environments.resolve(environment)
        while True:
            page = await self.list(
                environment_id, label=label, agent_name=agent_name, kind=kind, status=status, limit=page_size,
                cursor=cursor,
            )  # fmt: skip
            for run in page.runs:
                yield run
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

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
        policy = wait_policy(f"run {run_id}", timeout_seconds, poll_interval_seconds)
        environment_id: Optional[str] = None
        while True:
            try:
                if environment_id is None:
                    environment_id = await self._environments._resolve(environment, policy=policy)
                run = await self._read(
                    environment_id,
                    run_id,
                    max_retries=POLL_RETRIES,
                    timeout=policy.poll_timeout(self._environments._client.timeout),
                )
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
            idempotency_key=idempotency_key, remedy=RUN_FEWER_INLINE_TASKS,
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
                    return await envs._call("POST", path, SubmitResult, body=body, max_retries=0, remedy=SUBMIT_LESS)
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


def runs_list_params(
    label: Optional[str],
    agent_name: Optional[str],
    kind: Optional[str],
    status: Optional[str],
    limit: Optional[int],
    cursor: Optional[str],
) -> Dict[str, Any]:
    """The query of ``GET /{id}/runs``. A blank label or agent name is refused here, as the API refuses it."""
    params: Dict[str, Any] = {}
    for name, value in (("label", label), ("agent_name", agent_name)):
        if value is None:
            continue
        if not value.strip():
            raise InvalidArgument(f"{name} is blank; pass None to list runs of every {name.replace('_', ' ')}")
        params[name] = value.strip()
    for name, given in (("kind", kind), ("status", status), ("cursor", cursor)):
        if given is not None:
            params[name] = given
    if limit is not None:
        params["limit"] = limit
    return params


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
