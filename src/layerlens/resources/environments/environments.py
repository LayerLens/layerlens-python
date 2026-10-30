from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Type, Union, Mapping, Optional, Sequence
from pathlib import Path
from functools import cached_property

import httpx

from ._shared import (
    DEFAULT_POLL_SECONDS,
    DEFAULT_WAIT_SECONDS,
    ENVIRONMENT_PAGE_SIZE,
    ModelT,
    Deadline,
    TaskInput,
    ResendPolicy,
    IdempotencyKey,
    unwrap,
    next_poll,
    error_body,
    as_response,
    create_body,
    unwrap_list,
    is_object_id,
    is_transient,
    envelope_data,
    check_body_size,
    environments_path,
    new_idempotency_key,
    environment_conflict,
    require_api_key_client,
)
from ..._resource import SyncAPIResource, AsyncAPIResource
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
    """``client.environments``: the Automation API. Needs an Automation API key.

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
    ) -> httpx.Response:
        if body is not None:
            check_body_size(body)
        if idempotency_key is None:
            return as_response(
                self._client._request_cast(method, path, body=body, params=params, max_retries=max_retries)
            )
        headers = {"Idempotency-Key": idempotency_key}
        policy = ResendPolicy(self._client._max_retries if max_retries is None else max_retries)
        while True:
            try:
                return as_response(
                    self._client._request_cast(method, path, body=body, params=params, headers=headers, max_retries=0)
                )
            except LayerLensError as err:
                self._sleep(policy.after_error(err))

    def _call(self, method: str, path: str, model: Type[ModelT], *, key: Optional[str] = None, **kw: Any) -> ModelT:
        return unwrap(self._send(method, path, **kw), model, key=key)

    def resolve(self, environment: str) -> str:
        """The id of an environment given by id or by name. An id (24 lower-case hex digits) is returned as is."""
        if is_object_id(environment):
            return environment
        return _only_match(environment, self.list(name=environment))

    def create(
        self,
        name: str,
        type: str,
        *,
        description: str,
        config: Optional[Mapping[str, Any]] = None,
        seed: Optional[int] = None,
        if_not_exists: bool = False,
        idempotency_key: IdempotencyKey = None,
    ) -> CreatedEnvironment:
        """Create an environment and start minting its world from ``description``.

        With ``if_not_exists``, an environment of this name created from the same type,
        description, config and seed is returned with ``reused=True`` (rebuilt if its last
        build failed). Raises :class:`EnvironmentConflictError` when the name is taken by
        one this call cannot reuse; its ``differences`` names the inputs that differ.

        An agent builds the worlds of some types (``WorldType.built_by_agent``) from the
        description, as a one-world composite: ``config`` and ``seed`` are unused, and the
        result's ``unused`` says so. A failed build of one is not started again; delete it
        and create it again.
        """
        body = create_body(
            name=name, type=type, description=description, config=config, seed=seed, if_not_exists=if_not_exists
        )
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

        ``hide_members`` leaves out the environments a composite is built from.
        """
        params = _list_params(name=name, search=search, hide_members=hide_members)
        found: List[Environment] = []
        page = 1
        while True:
            response = self._send("GET", self._path(), params={**params, "page": page})
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
        fails, and :class:`TimeoutError` when it is still building after ``timeout_seconds``.
        """
        environment_id = self.resolve(environment)
        deadline = Deadline(timeout_seconds)
        interval = poll_interval_seconds
        while True:
            try:
                build = self.build_status(environment_id)
            except LayerLensError as err:
                if not is_transient(err) or deadline.passed():
                    raise
            else:
                if build.ready:
                    return self.get(environment_id)
                if build.failed:
                    raise EnvironmentBuildError(environment_id, build)
                if deadline.passed():
                    raise TimeoutError(f"environment {environment_id} is still building after {timeout_seconds:g}s")
            self._sleep(deadline.clip(interval))
            interval = next_poll(interval)

    def world_types(self) -> List[WorldType]:
        """The world types an environment can simulate, with their config knobs."""
        response = self._send("GET", self._path("world-types"))
        return _world_types(response)

    def tools(self, environment: str) -> ToolList:
        return self._call("GET", self._path(self.resolve(environment), "tools"), ToolList)

    def reset_state(self, environment: str) -> StateReset:
        """Clear the live state, the one Agent API calls write to, back to the minted world."""
        return self._call("DELETE", self._path(self.resolve(environment), "state"), StateReset)

    def revert(self, environment: str, *, to_version: int) -> EnvironmentRef:
        """Serve an earlier version of the world. Already serving ``to_version`` is not an error."""
        environment_id = self.resolve(environment)
        try:
            return self._call(
                "POST", self._path(environment_id, "revert"), EnvironmentRef,
                key="environment", body={"to_version": to_version},
            )  # fmt: skip
        except ConflictError:
            found = self._call("GET", self._path(environment_id), Environment, key="environment")
            if found.current_version != to_version:
                raise
            return EnvironmentRef(id=found.id, name=found.name, type=found.type, current_version=found.current_version)

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
    ) -> httpx.Response:
        if body is not None:
            check_body_size(body)
        if idempotency_key is None:
            return as_response(
                await self._client._request_cast(method, path, body=body, params=params, max_retries=max_retries)
            )
        headers = {"Idempotency-Key": idempotency_key}
        policy = ResendPolicy(self._client._max_retries if max_retries is None else max_retries)
        while True:
            try:
                return as_response(
                    await self._client._request_cast(
                        method, path, body=body, params=params, headers=headers, max_retries=0
                    )
                )
            except LayerLensError as err:
                await self._sleep(policy.after_error(err))

    async def _call(
        self, method: str, path: str, model: Type[ModelT], *, key: Optional[str] = None, **kw: Any
    ) -> ModelT:
        return unwrap(await self._send(method, path, **kw), model, key=key)

    async def resolve(self, environment: str) -> str:
        if is_object_id(environment):
            return environment
        return _only_match(environment, await self.list(name=environment))

    async def create(
        self,
        name: str,
        type: str,
        *,
        description: str,
        config: Optional[Mapping[str, Any]] = None,
        seed: Optional[int] = None,
        if_not_exists: bool = False,
        idempotency_key: IdempotencyKey = None,
    ) -> CreatedEnvironment:
        body = create_body(
            name=name, type=type, description=description, config=config, seed=seed, if_not_exists=if_not_exists
        )
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
        params = _list_params(name=name, search=search, hide_members=hide_members)
        found: List[Environment] = []
        page = 1
        while True:
            response = await self._send("GET", self._path(), params={**params, "page": page})
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
        environment_id = await self.resolve(environment)
        deadline = Deadline(timeout_seconds)
        interval = poll_interval_seconds
        while True:
            try:
                build = await self.build_status(environment_id)
            except LayerLensError as err:
                if not is_transient(err) or deadline.passed():
                    raise
            else:
                if build.ready:
                    return await self.get(environment_id)
                if build.failed:
                    raise EnvironmentBuildError(environment_id, build)
                if deadline.passed():
                    raise TimeoutError(f"environment {environment_id} is still building after {timeout_seconds:g}s")
            await self._sleep(deadline.clip(interval))
            interval = next_poll(interval)

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
        except ConflictError:
            found = await self._call("GET", self._path(environment_id), Environment, key="environment")
            if found.current_version != to_version:
                raise
            return EnvironmentRef(id=found.id, name=found.name, type=found.type, current_version=found.current_version)

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
