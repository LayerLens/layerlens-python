from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ._shared import DEFAULT_POLL_SECONDS, DEFAULT_WAIT_SECONDS, Deadline, next_poll, is_transient
from ..._exceptions import LayerLensError
from ...models.environment import Job

if TYPE_CHECKING:
    from .environments import Environments, AsyncEnvironments


class Jobs:
    """``client.environments.jobs``: builds, task generations and the environment's other background jobs."""

    def __init__(self, environments: Environments) -> None:
        self._environments = environments

    def get(self, environment: str, job_id: str) -> Job:
        envs = self._environments
        return envs._call("GET", envs._path(envs.resolve(environment), "jobs", job_id), Job)

    def wait(
        self,
        environment: str,
        job_id: str,
        *,
        timeout_seconds: Optional[float] = DEFAULT_WAIT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> Job:
        """Block until the job finishes, and return it, whatever its ``status``.

        Raises :class:`TimeoutError` when it is still running after ``timeout_seconds``.
        """
        environment_id = self._environments.resolve(environment)
        deadline = Deadline(timeout_seconds)
        interval = poll_interval_seconds
        while True:
            try:
                job = self.get(environment_id, job_id)
            except LayerLensError as err:
                if not is_transient(err) or deadline.passed():
                    raise
            else:
                if job.finished:
                    return job
                if deadline.passed():
                    raise TimeoutError(f"job {job_id} is still {job.status} after {timeout_seconds:g}s")
            self._environments._sleep(deadline.clip(interval))
            interval = next_poll(interval)


class AsyncJobs:
    """``client.environments.jobs`` on :class:`AsyncLayerLens`. See :class:`Jobs`."""

    def __init__(self, environments: AsyncEnvironments) -> None:
        self._environments = environments

    async def get(self, environment: str, job_id: str) -> Job:
        envs = self._environments
        return await envs._call("GET", envs._path(await envs.resolve(environment), "jobs", job_id), Job)

    async def wait(
        self,
        environment: str,
        job_id: str,
        *,
        timeout_seconds: Optional[float] = DEFAULT_WAIT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> Job:
        environment_id = await self._environments.resolve(environment)
        deadline = Deadline(timeout_seconds)
        interval = poll_interval_seconds
        while True:
            try:
                job = await self.get(environment_id, job_id)
            except LayerLensError as err:
                if not is_transient(err) or deadline.passed():
                    raise
            else:
                if job.finished:
                    return job
                if deadline.passed():
                    raise TimeoutError(f"job {job_id} is still {job.status} after {timeout_seconds:g}s")
            await self._environments._sleep(deadline.clip(interval))
            interval = next_poll(interval)
