from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from ._shared import DEFAULT_POLL_SECONDS, DEFAULT_WAIT_SECONDS, still, wait_policy
from ..._polling import POLL_RETRIES
from ..._resource import AsyncOrganizationScoped
from ..._exceptions import LayerLensError
from ...models.environment import Job

if TYPE_CHECKING:
    from .environments import Environments, AsyncEnvironments


class Jobs:
    """``client.environments.jobs``: builds, task generations and the environment's other background jobs."""

    def __init__(self, environments: Environments) -> None:
        self._environments = environments

    def get(self, environment: str, job_id: str) -> Job:
        return self._read(self._environments.resolve(environment), job_id)

    def _read(self, environment_id: str, job_id: str, **request: Any) -> Job:
        envs = self._environments
        return envs._call("GET", envs._path(environment_id, "jobs", job_id), Job, **request)

    def wait(
        self,
        environment: str,
        job_id: str,
        *,
        timeout_seconds: Optional[float] = DEFAULT_WAIT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> Job:
        """Block until the job finishes, and return it, whatever its ``status``.

        A poll that fails on a 429, a 5xx or a lost connection does not end the wait.
        Raises :class:`TimeoutError` when the job is still running after
        ``timeout_seconds``, or the polls kept failing until then.
        """
        environment_id = self._environments.resolve(environment)
        policy = wait_policy(f"job {job_id}", timeout_seconds, poll_interval_seconds)
        while True:
            try:
                job = self._read(environment_id, job_id, max_retries=POLL_RETRIES)
            except LayerLensError as err:
                delay = policy.after_error(err)
            else:
                if job.finished:
                    return job
                delay = policy.after_pending(still(f"job {job_id} is still {job.status}", timeout_seconds))
            self._environments._sleep(delay)


class AsyncJobs(AsyncOrganizationScoped):
    """``client.environments.jobs`` on :class:`AsyncLayerLens`. See :class:`Jobs`."""

    def __init__(self, environments: AsyncEnvironments) -> None:
        self._environments = environments

    def _organization_client(self) -> Any:
        return self._environments._client

    async def get(self, environment: str, job_id: str) -> Job:
        return await self._read(await self._environments.resolve(environment), job_id)

    async def _read(self, environment_id: str, job_id: str, **request: Any) -> Job:
        envs = self._environments
        return await envs._call("GET", envs._path(environment_id, "jobs", job_id), Job, **request)

    async def wait(
        self,
        environment: str,
        job_id: str,
        *,
        timeout_seconds: Optional[float] = DEFAULT_WAIT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> Job:
        environment_id = await self._environments.resolve(environment)
        policy = wait_policy(f"job {job_id}", timeout_seconds, poll_interval_seconds)
        while True:
            try:
                job = await self._read(environment_id, job_id, max_retries=POLL_RETRIES)
            except LayerLensError as err:
                delay = policy.after_error(err)
            else:
                if job.finished:
                    return job
                delay = policy.after_pending(still(f"job {job_id} is still {job.status}", timeout_seconds))
            await self._environments._sleep(delay)
