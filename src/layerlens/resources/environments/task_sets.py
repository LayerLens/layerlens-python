from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Tuple, Optional
from urllib.parse import quote

import httpx

from ._shared import (
    TASK_SET_PAGE_SIZE,
    IdempotencyKey,
    InvalidArgument,
    unwrap_list,
    envelope_data,
    new_idempotency_key,
)
from ..._resource import AsyncOrganizationScoped
from ..._exceptions import APIResponseValidationError
from ...models.environment import Job, Task, TaskSet, TaskSetSummary

if TYPE_CHECKING:
    from .environments import Environments, AsyncEnvironments


class TaskSets:
    """``client.environments.task_sets``: the named task sets stored in an environment."""

    def __init__(self, environments: Environments) -> None:
        self._environments = environments

    def list(self, environment: str) -> List[TaskSetSummary]:
        """The environment's task sets, with the number of tasks that are not retired."""
        envs = self._environments
        response = envs._send("GET", envs._path(envs.resolve(environment), "task-sets"))
        return unwrap_list(response, TaskSetSummary, "task_sets")

    def get(self, environment: str, set_name: str) -> TaskSet:
        """One set and all of its tasks, retired ones included and marked."""
        envs = self._environments
        path = envs._path(envs.resolve(environment), "task-sets", _segment(set_name))
        tasks: List[Task] = []
        offset: Optional[int] = 0
        total = 0
        while offset is not None:
            response = envs._send("GET", path, params={"limit": TASK_SET_PAGE_SIZE, "offset": offset})
            total, offset = _page(response, tasks, offset)
        return TaskSet(name=set_name, total=total, tasks=tasks)

    def delete(self, environment: str, set_name: str) -> int:
        """Delete every task of the set; returns how many were deleted (0 for a set that does not exist)."""
        envs = self._environments
        response = envs._send("DELETE", envs._path(envs.resolve(environment), "task-sets", _segment(set_name)))
        return _deleted(response)

    def generate(
        self,
        environment: str,
        set_name: str,
        *,
        count: int,
        budget_usd: float,
        prompt: Optional[str] = None,
        target: Optional[str] = None,
        model: Optional[str] = None,
        idempotency_key: IdempotencyKey = None,
    ) -> Job:
        """Generate tasks into the set from the environment's answer key; returns the job.

        ``target`` is ``"discriminating"`` (the default), ``"hard"`` or ``"any"``. ``prompt``
        defaults to the environment's description. Wait for it with ``jobs.wait``.
        """
        envs = self._environments
        return envs._call(
            "POST", envs._path(envs.resolve(environment), "task-sets", _segment(set_name), "generate"), Job,
            body=_generate_body(count, budget_usd, prompt, target, model),
            idempotency_key=idempotency_key or new_idempotency_key(),
        )  # fmt: skip


class AsyncTaskSets(AsyncOrganizationScoped):
    """``client.environments.task_sets`` on :class:`AsyncLayerLens`. See :class:`TaskSets`."""

    def __init__(self, environments: AsyncEnvironments) -> None:
        self._environments = environments

    def _organization_client(self) -> Any:
        return self._environments._client

    async def list(self, environment: str) -> List[TaskSetSummary]:
        envs = self._environments
        response = await envs._send("GET", envs._path(await envs.resolve(environment), "task-sets"))
        return unwrap_list(response, TaskSetSummary, "task_sets")

    async def get(self, environment: str, set_name: str) -> TaskSet:
        envs = self._environments
        path = envs._path(await envs.resolve(environment), "task-sets", _segment(set_name))
        tasks: List[Task] = []
        offset: Optional[int] = 0
        total = 0
        while offset is not None:
            response = await envs._send("GET", path, params={"limit": TASK_SET_PAGE_SIZE, "offset": offset})
            total, offset = _page(response, tasks, offset)
        return TaskSet(name=set_name, total=total, tasks=tasks)

    async def delete(self, environment: str, set_name: str) -> int:
        envs = self._environments
        path = envs._path(await envs.resolve(environment), "task-sets", _segment(set_name))
        return _deleted(await envs._send("DELETE", path))

    async def generate(
        self,
        environment: str,
        set_name: str,
        *,
        count: int,
        budget_usd: float,
        prompt: Optional[str] = None,
        target: Optional[str] = None,
        model: Optional[str] = None,
        idempotency_key: IdempotencyKey = None,
    ) -> Job:
        envs = self._environments
        return await envs._call(
            "POST", envs._path(await envs.resolve(environment), "task-sets", _segment(set_name), "generate"), Job,
            body=_generate_body(count, budget_usd, prompt, target, model),
            idempotency_key=idempotency_key or new_idempotency_key(),
        )  # fmt: skip


def _segment(set_name: str) -> str:
    if not set_name or "/" in set_name:
        raise InvalidArgument(f"a task set name is non-empty and holds no '/', got {set_name!r}")
    return quote(set_name, safe="")


def _page(response: httpx.Response, into: List[Task], offset: int) -> Tuple[int, Optional[int]]:
    data = envelope_data(response)
    into.extend(unwrap_list(response, Task, "tasks"))
    total, next_offset = data.get("total"), data.get("next_offset")
    if not isinstance(total, int) or not (next_offset is None or isinstance(next_offset, int)):
        raise APIResponseValidationError(response, data, message="the task set page has no total or next_offset")
    if next_offset is not None and next_offset <= offset:
        raise APIResponseValidationError(
            response,
            data,
            message=f"the task set page at offset {offset} gives next_offset {next_offset}, which does not move "
            "forward, so the set cannot be read to its end; try again, and report it if it persists",
        )
    return total, next_offset


def _deleted(response: httpx.Response) -> int:
    data = envelope_data(response)
    deleted = data.get("deleted") if isinstance(data, dict) else None
    if not isinstance(deleted, int):
        raise APIResponseValidationError(response, data, message="the reply has no 'deleted' count")
    return deleted


def _generate_body(
    count: int, budget_usd: float, prompt: Optional[str], target: Optional[str], model: Optional[str]
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"count": count, "budget_usd": budget_usd}
    if prompt is not None:
        body["prompt"] = prompt
    if target is not None:
        body["target"] = target
    if model is not None:
        body["model"] = model
    return body
