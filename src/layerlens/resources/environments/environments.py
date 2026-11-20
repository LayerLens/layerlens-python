from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Type, Union, Optional, Sequence
from pathlib import Path
from functools import cached_property

import httpx

from ._shared import (
    SEND_LESS_AT_ONCE,
    DEFAULT_POLL_SECONDS,
    DEFAULT_WAIT_SECONDS,
    ENVIRONMENT_PAGE_SIZE,
    ModelT,
    TaskInput,
    ResendPolicy,
    IdempotencyKey,
    still,
    unwrap,
    error_body,
    as_response,
    create_body,
    unwrap_list,
    wait_policy,
    is_object_id,
    envelope_data,
    check_body_size,
    environments_path,
    new_idempotency_key,
    environment_conflict,
    require_api_key_client,
)
from ..._polling import POLL_RETRIES, PollPolicy
from ..._resource import SyncAPIResource, AsyncAPIResource
from ..._constants import NOT_GIVEN, NotGiven
from ..._exceptions import ConflictError, NotFoundError, LayerLensError, EnvironmentBuildError
from ...models.environment import (
    ToolList,
    WorldType,
    StateReset,
    Environment,
    EnvironmentRef,
    EnvironmentBuild,
    CreatedEnvironment,
)

if TYPE_CHECKING:
    from .jobs import Jobs, AsyncJobs
    from .runs import Runs, AsyncRuns
    from ..._client import LayerLens, AsyncLayerLens
    from .task_sets import TaskSets, AsyncTaskSets
    from .external_run import ExternalRun, AsyncExternalRun


class Environments(SyncAPIResource):
    """``client.environments``: the Automation API. Needs an API key: one with Automation access
    for most methods; one with Standard access reaches only what an agent uses (see
    docs/api-reference/environments.md), and is refused the rest with ``PermissionDeniedError``.

    Wherever a method takes ``environment``, pass the environment's id or its name.
    """

    def __init__(self, client: LayerLens) -> None:
        require_api_key_client(client._use_bearer_auth)
        super().__init__(client)

    @cached_property
    def task_sets(self) -> TaskSets:
        from .task_sets import TaskSets

        return TaskSets(self)

    @cached_property
    def jobs(self) -> Jobs:
        from .jobs import Jobs

        return Jobs(self)

    @cached_property
    def runs(self) -> Runs:
        from .runs import Runs

        return Runs(self)

    def _path(self, *parts: str) -> str:
        return "/".join((environments_path(self._client.organization_id, self._client.project_id), *parts))

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        idempotency_key: IdempotencyKey = None,
        max_retries: Optional[int] = None,
        remedy: str = SEND_LESS_AT_ONCE,
        timeout: Union[float, httpx.Timeout, None, NotGiven] = NOT_GIVEN,
    ) -> httpx.Response:
        if body is not None:
            check_body_size(body, remedy=remedy)
        if idempotency_key is None:
            return as_response(
                self._client._request_cast(
                    method, path, body=body, params=params, max_retries=max_retries, timeout=timeout
                )
            )
        headers = {"Idempotency-Key": idempotency_key}
        policy = ResendPolicy(self._client._max_retries if max_retries is None else max_retries)
        while True:
            try:
                return as_response(
                    self._client._request_cast(
                        method, path, body=body, params=params, headers=headers, max_retries=0, timeout=timeout
                    )
                )
            except LayerLensError as err:
                self._sleep(policy.after_error(err))

    def _call(self, method: str, path: str, model: Type[ModelT], *, key: Optional[str] = None, **kw: Any) -> ModelT:
        return unwrap(self._send(method, path, **kw), model, key=key)

    def resolve(self, environment: str) -> str:
        """The id of an environment given by id or by name. An id (24 lower-case hex digits) is returned as is."""
        return self._resolve(environment)

    def _resolve(self, environment: str, *, policy: Optional[PollPolicy] = None) -> str:
        """``resolve``; within a wait (``policy``), each page is sent as a poll is (see ``_list``)."""
        if is_object_id(environment):
            return environment
        return _only_match(
            environment, self._list(_list_params(name=environment, search=None, hide_members=False), policy=policy)
        )

    def create(
        self,
        name: str,
        type: str,
        *,
        description: str,
        if_not_exists: bool = False,
        idempotency_key: IdempotencyKey = None,
    ) -> CreatedEnvironment:
        """Create an environment and start building its world from ``description``.

        An agent reads the company the description is about and builds that company's
        ``type`` app (a type whose ``WorldType.built_by_agent`` is true; any other is refused
        with :class:`BadRequestError`). The environment is a composite of that one app: its
        ``type`` is ``"composite"`` and its tools are named ``<alias>__<tool>``. An API key works
        with the composite only: its environment list leaves the app out, and the agent and
        Automation routes refuse the app with a 409 naming its composite.

        With ``if_not_exists``, an environment of this name created from the same type and
        description is returned with ``reused=True``. Raises :class:`EnvironmentConflictError`
        when the name is taken by one this call cannot reuse: its ``differences`` names the
        inputs that differ; when it is empty, the message says why, such as a last build that
        failed. A failed build is never started again: delete the environment and create it
        again.
        """
        body = create_body(name=name, type=type, description=description, if_not_exists=if_not_exists)
        try:
            return self._call(
                "POST", self._path("worlds"), CreatedEnvironment, body=body,
                idempotency_key=idempotency_key or new_idempotency_key(),
            )  # fmt: skip
        except ConflictError as err:
            raise environment_conflict(err) from None

    def get(self, environment: str) -> Environment:
        return self._call("GET", self._path(self.resolve(environment)), Environment, key="environment")

    def list(
        self, *, name: Optional[str] = None, search: Optional[str] = None, hide_members: bool = False
    ) -> List[Environment]:
        """The project's environments, newest change first; ``name`` finds one by its exact name.

        ``hide_members`` is kept for compatibility: an API key's list already leaves out a
        composite's apps, so it changes nothing.
        """
        return self._list(_list_params(name=name, search=search, hide_members=hide_members))

    def _list(self, params: Dict[str, Any], *, policy: Optional[PollPolicy] = None) -> List[Environment]:
        """Every page. Within a wait (``policy``), each page is sent once, with the time left of
        the wait when it is sent; a failure goes to the wait, which retries the lookup."""
        found: List[Environment] = []
        page = 1
        while True:
            response = self._send(
                "GET", self._path(), params={**params, "page": page}, **_as_poll(policy, self._client)
            )
            if policy is not None:
                policy.answered()
            batch = unwrap_list(response, Environment, "environments")
            found.extend(batch)
            if not batch or len(found) >= _total_count(response):
                return found
            page += 1

    def build_status(self, environment: str) -> EnvironmentBuild:
        return self._call("GET", self._path(self.resolve(environment), "build"), EnvironmentBuild)

    def wait_until_ready(
        self,
        environment: str,
        *,
        timeout_seconds: Optional[float] = DEFAULT_WAIT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> Environment:
        """Block until the environment's build finishes, and return the environment.

        Raises :class:`EnvironmentBuildError` with the generator's reason when the build
        fails, and :class:`TimeoutError` when it is still building after ``timeout_seconds``,
        or the polls kept failing until then; a poll that fails on a 429, a 5xx or a lost
        connection does not end the wait.

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
        self._client._organization_scope()
        policy = wait_policy(f"environment {environment}", timeout_seconds, poll_interval_seconds)
        environment_id: Optional[str] = None
        while True:
            try:
                if environment_id is None:
                    environment_id = self._resolve(environment, policy=policy)
                ready = self._ready(environment_id, policy)
            except EnvironmentBuildError:
                raise
            except LayerLensError as err:
                delay = policy.after_error(err)
            else:
                if ready is not None:
                    return ready
                delay = policy.after_pending(still(f"environment {environment_id} is still building", timeout_seconds))
            self._sleep(delay)

    def _ready(self, environment_id: str, policy: PollPolicy) -> Optional[Environment]:
        """One poll of a build: the environment once it is ready, ``None`` while it builds.

        Each of its two reads is sent with the time left of the wait (``policy.poll_timeout``).
        """
        build = self._call(
            "GET", self._path(environment_id, "build"), EnvironmentBuild,
            max_retries=POLL_RETRIES, timeout=policy.poll_timeout(self._client.timeout),
        )  # fmt: skip
        if build.failed:
            raise EnvironmentBuildError(environment_id, build)
        if not build.ready:
            return None
        policy.answered()
        return self._call(
            "GET", self._path(environment_id), Environment, key="environment",
            max_retries=POLL_RETRIES, timeout=policy.poll_timeout(self._client.timeout),
        )  # fmt: skip

    def world_types(self) -> List[WorldType]:
        """The world types an environment can simulate: the apps an agent builds from a description."""
        response = self._send("GET", self._path("world-types"))
        return _world_types(response)

    def tools(self, environment: str) -> ToolList:
        return self._call("GET", self._path(self.resolve(environment), "tools"), ToolList)

    def reset_state(self, environment: str) -> StateReset:
        """Clear the live state, the one Agent API calls write to, back to the minted world."""
        return self._call("DELETE", self._path(self.resolve(environment), "state"), StateReset)

    def revert(self, environment: str, *, to_version: int) -> EnvironmentRef:
        """Serve an earlier version of the world. Already serving ``to_version`` is not an error.

        The API refuses (409) a revert while a build or a task generation runs, and a revert to
        the version already served; only the second is success, and only when the environment
        really serves that version: its tools are there and no build runs.
        """
        environment_id = self.resolve(environment)
        try:
            return self._call(
                "POST", self._path(environment_id, "revert"), EnvironmentRef,
                key="environment", body={"to_version": to_version},
            )  # fmt: skip
        except ConflictError as err:
            if _refused_while_busy(err):
                raise
            found = self._call("GET", self._path(environment_id), Environment, key="environment")
            if not _serves(found, to_version) or _building(self._latest_build(environment_id)):
                raise
            return EnvironmentRef(id=found.id, name=found.name, type=found.type, current_version=found.current_version)

    def _latest_build(self, environment_id: str) -> Optional[EnvironmentBuild]:
        """The latest build, or None for an environment that has none (an OpenAPI spec import)."""
        try:
            return self._call("GET", self._path(environment_id, "build"), EnvironmentBuild)
        except NotFoundError:
            return None

    def delete(self, environment: str) -> None:
        """Delete an environment. One that is gone once this returns is deleted, whoever deleted it."""
        environment_id = self.resolve(environment)
        if is_object_id(environment):
            self._call("GET", self._path(environment_id), Environment, key="environment")
        try:
            envelope_data(self._send("DELETE", self._path(environment_id)))
        except NotFoundError as err:
            if error_body(err.response).get("status") != "error":
                raise

    def external_run(
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
    ) -> ExternalRun:
        """A graded run of your own agent, one attempt per task. Use it as a context manager.

        The run starts on entering the ``with`` block (or on :meth:`ExternalRun.start`).
        Leaving the block normally finishes it, grading unsubmitted attempts ``error``;
        leaving it on an exception cancels it.
        """
        from .external_run import ExternalRun

        return ExternalRun(
            self.runs, environment, agent_name=agent_name, set_name=set_name, tasks=tasks, tasks_file=tasks_file,
            task_names=task_names, label=label, idempotency_key=idempotency_key,
        )  # fmt: skip


class AsyncEnvironments(AsyncAPIResource):
    """``client.environments`` on :class:`AsyncLayerLens`. See :class:`Environments`."""

    def __init__(self, client: AsyncLayerLens) -> None:
        require_api_key_client(client._use_bearer_auth)
        super().__init__(client)

    @cached_property
    def task_sets(self) -> AsyncTaskSets:
        from .task_sets import AsyncTaskSets

        return AsyncTaskSets(self)

    @cached_property
    def jobs(self) -> AsyncJobs:
        from .jobs import AsyncJobs

        return AsyncJobs(self)

    @cached_property
    def runs(self) -> AsyncRuns:
        from .runs import AsyncRuns

        return AsyncRuns(self)

    def _path(self, *parts: str) -> str:
        return "/".join((environments_path(self._client.organization_id, self._client.project_id), *parts))

    async def _send(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        idempotency_key: IdempotencyKey = None,
        max_retries: Optional[int] = None,
        remedy: str = SEND_LESS_AT_ONCE,
        timeout: Union[float, httpx.Timeout, None, NotGiven] = NOT_GIVEN,
    ) -> httpx.Response:
        if body is not None:
            check_body_size(body, remedy=remedy)
        if idempotency_key is None:
            return as_response(
                await self._client._request_cast(
                    method, path, body=body, params=params, max_retries=max_retries, timeout=timeout
                )
            )
        headers = {"Idempotency-Key": idempotency_key}
        policy = ResendPolicy(self._client._max_retries if max_retries is None else max_retries)
        while True:
            try:
                return as_response(
                    await self._client._request_cast(
                        method, path, body=body, params=params, headers=headers, max_retries=0, timeout=timeout
                    )
                )
            except LayerLensError as err:
                await self._sleep(policy.after_error(err))

    async def _call(
        self, method: str, path: str, model: Type[ModelT], *, key: Optional[str] = None, **kw: Any
    ) -> ModelT:
        return unwrap(await self._send(method, path, **kw), model, key=key)

    async def resolve(self, environment: str) -> str:
        return await self._resolve(environment)

    async def _resolve(self, environment: str, *, policy: Optional[PollPolicy] = None) -> str:
        if is_object_id(environment):
            return environment
        return _only_match(
            environment,
            await self._list(_list_params(name=environment, search=None, hide_members=False), policy=policy),
        )

    async def create(
        self,
        name: str,
        type: str,
        *,
        description: str,
        if_not_exists: bool = False,
        idempotency_key: IdempotencyKey = None,
    ) -> CreatedEnvironment:
        """Create an environment and start building its world. See :meth:`Environments.create`."""
        body = create_body(name=name, type=type, description=description, if_not_exists=if_not_exists)
        try:
            return await self._call(
                "POST", self._path("worlds"), CreatedEnvironment, body=body,
                idempotency_key=idempotency_key or new_idempotency_key(),
            )  # fmt: skip
        except ConflictError as err:
            raise environment_conflict(err) from None

    async def get(self, environment: str) -> Environment:
        return await self._call("GET", self._path(await self.resolve(environment)), Environment, key="environment")

    async def list(
        self, *, name: Optional[str] = None, search: Optional[str] = None, hide_members: bool = False
    ) -> List[Environment]:
        """The project's environments (see :meth:`Environments.list`; ``hide_members`` changes nothing)."""
        return await self._list(_list_params(name=name, search=search, hide_members=hide_members))

    async def _list(self, params: Dict[str, Any], *, policy: Optional[PollPolicy] = None) -> List[Environment]:
        found: List[Environment] = []
        page = 1
        while True:
            response = await self._send(
                "GET", self._path(), params={**params, "page": page}, **_as_poll(policy, self._client)
            )
            if policy is not None:
                policy.answered()
            batch = unwrap_list(response, Environment, "environments")
            found.extend(batch)
            if not batch or len(found) >= _total_count(response):
                return found
            page += 1

    async def build_status(self, environment: str) -> EnvironmentBuild:
        return await self._call("GET", self._path(await self.resolve(environment), "build"), EnvironmentBuild)

    async def wait_until_ready(
        self,
        environment: str,
        *,
        timeout_seconds: Optional[float] = DEFAULT_WAIT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> Environment:
        policy = wait_policy(f"environment {environment}", timeout_seconds, poll_interval_seconds)
        environment_id: Optional[str] = None
        while True:
            try:
                if environment_id is None:
                    environment_id = await self._resolve(environment, policy=policy)
                ready = await self._ready(environment_id, policy)
            except EnvironmentBuildError:
                raise
            except LayerLensError as err:
                delay = policy.after_error(err)
            else:
                if ready is not None:
                    return ready
                delay = policy.after_pending(still(f"environment {environment_id} is still building", timeout_seconds))
            await self._sleep(delay)

    async def _ready(self, environment_id: str, policy: PollPolicy) -> Optional[Environment]:
        build = await self._call(
            "GET", self._path(environment_id, "build"), EnvironmentBuild,
            max_retries=POLL_RETRIES, timeout=policy.poll_timeout(self._client.timeout),
        )  # fmt: skip
        if build.failed:
            raise EnvironmentBuildError(environment_id, build)
        if not build.ready:
            return None
        policy.answered()
        return await self._call(
            "GET", self._path(environment_id), Environment, key="environment",
            max_retries=POLL_RETRIES, timeout=policy.poll_timeout(self._client.timeout),
        )  # fmt: skip

    async def world_types(self) -> List[WorldType]:
        return _world_types(await self._send("GET", self._path("world-types")))

    async def tools(self, environment: str) -> ToolList:
        return await self._call("GET", self._path(await self.resolve(environment), "tools"), ToolList)

    async def reset_state(self, environment: str) -> StateReset:
        return await self._call("DELETE", self._path(await self.resolve(environment), "state"), StateReset)

    async def revert(self, environment: str, *, to_version: int) -> EnvironmentRef:
        environment_id = await self.resolve(environment)
        try:
            return await self._call(
                "POST", self._path(environment_id, "revert"), EnvironmentRef,
                key="environment", body={"to_version": to_version},
            )  # fmt: skip
        except ConflictError as err:
            if _refused_while_busy(err):
                raise
            found = await self._call("GET", self._path(environment_id), Environment, key="environment")
            if not _serves(found, to_version) or _building(await self._latest_build(environment_id)):
                raise
            return EnvironmentRef(id=found.id, name=found.name, type=found.type, current_version=found.current_version)

    async def _latest_build(self, environment_id: str) -> Optional[EnvironmentBuild]:
        try:
            return await self._call("GET", self._path(environment_id, "build"), EnvironmentBuild)
        except NotFoundError:
            return None

    async def delete(self, environment: str) -> None:
        environment_id = await self.resolve(environment)
        if is_object_id(environment):
            await self._call("GET", self._path(environment_id), Environment, key="environment")
        try:
            envelope_data(await self._send("DELETE", self._path(environment_id)))
        except NotFoundError as err:
            if error_body(err.response).get("status") != "error":
                raise

    def external_run(
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
    ) -> AsyncExternalRun:
        """A graded run of your own agent; use it with ``async with``. See :meth:`Environments.external_run`."""
        from .external_run import AsyncExternalRun

        return AsyncExternalRun(
            self.runs, environment, agent_name=agent_name, set_name=set_name, tasks=tasks, tasks_file=tasks_file,
            task_names=task_names, label=label, idempotency_key=idempotency_key,
        )  # fmt: skip


def _refused_while_busy(err: ConflictError) -> bool:
    """The API refused because a build or a task generation runs.

    Its sentence then asks to wait: "... is still being built; wait for that build to finish
    before changing it", "... is generating tasks from its current version; wait for that to
    finish before changing it". Nothing else tells an API key that a task generation runs (it
    cannot list jobs), and the version served is the one asked for then, so without this a
    revert during a generation reported success while the API had refused it (R6 F4).
    """
    sentence = err.body if isinstance(err.body, str) else err.message
    return "wait for" in sentence


def _serves(found: Environment, to_version: int) -> bool:
    """The environment serves ``to_version``: its pointer and the version it serves are that
    version, and it has tools. While the first build runs both are 1 with no tools (R6 F4)."""
    return found.current_version == to_version and found.environment_version == to_version and found.tool_count > 0


def _building(build: Optional[EnvironmentBuild]) -> bool:
    """A build is running: its tools and pointer may still change. A failed build leaves the
    version served before it as it was."""
    return build is not None and not build.ready and not build.failed


def _as_poll(policy: Optional[PollPolicy], client: Any) -> Dict[str, Any]:
    """How a request of a wait is sent: once, with the time left of the wait. Nothing outside one."""
    if policy is None:
        return {}
    return {"max_retries": POLL_RETRIES, "timeout": policy.poll_timeout(client.timeout)}


def _only_match(name: str, matches: List[Environment]) -> str:
    if not matches:
        raise LayerLensError(f"this project has no environment named {name!r}")
    if len(matches) > 1:
        ids = ", ".join(m.id for m in matches)
        raise LayerLensError(f"more than one environment is named {name!r} ({ids}); pass its id instead")
    return matches[0].id


def _list_params(*, name: Optional[str], search: Optional[str], hide_members: bool) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": ENVIRONMENT_PAGE_SIZE}
    if name is not None:
        params["name"] = name
    if search is not None:
        params["search"] = search
    if hide_members:
        params["hide_members"] = "true"
    return params


def _total_count(response: httpx.Response) -> int:
    data = envelope_data(response)
    total = data.get("total_count") if isinstance(data, dict) else None
    return total if isinstance(total, int) else 0


def _world_types(response: httpx.Response) -> List[WorldType]:
    data = envelope_data(response)
    unavailable = data.get("unavailable") if isinstance(data, dict) else None
    if unavailable:
        raise LayerLensError(str(unavailable))
    return unwrap_list(response, WorldType, "world_types")
