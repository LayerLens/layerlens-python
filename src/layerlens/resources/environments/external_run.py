from __future__ import annotations

import logging
import threading
from types import TracebackType
from typing import TYPE_CHECKING, Any, Dict, List, Type, Union, Optional, Sequence
from pathlib import Path
from datetime import datetime

from .runs import external_run_body
from ._shared import DEFAULT_POLL_SECONDS, DEFAULT_WAIT_SECONDS, TaskInput, IdempotencyKey, new_idempotency_key
from ..._exceptions import ConflictError, LayerLensError
from ...models.environment import RunResult, SubmitResult, ExternalRunStart, ExternalRunAttempt

if TYPE_CHECKING:
    from .runs import Runs, AsyncRuns

log: logging.Logger = logging.getLogger(__name__)


class _AttemptFields:
    _data: ExternalRunAttempt

    @property
    def attempt_id(self) -> str:
        return self._data.attempt_id

    @property
    def task_id(self) -> str:
        return self._data.task_id

    @property
    def task_name(self) -> str:
        return self._data.task_name

    @property
    def prompt(self) -> str:
        return self._data.prompt

    @property
    def base_url(self) -> Optional[str]:
        """The attempt's address: ``GET {base_url}/tools`` and ``POST {base_url}/call``."""
        return self._data.base_url

    @property
    def token(self) -> str:
        """The attempt's credential, as ``x-api-key``, ``Authorization: Bearer`` or the Basic password."""
        return self._data.token

    @property
    def call_url(self) -> Optional[str]:
        return self._data.call_url

    @property
    def expires_at(self) -> datetime:
        return self._data.expires_at

    def __repr__(self) -> str:
        return f"{type(self).__name__}(attempt_id={self.attempt_id!r}, task_name={self.task_name!r})"


class ExternalAttempt(_AttemptFields):
    """One task of a run of your own agent: its prompt, address and token, and its submit."""

    def __init__(self, run: ExternalRun, data: ExternalRunAttempt) -> None:
        self._run = run
        self._data = data
        self._lock = threading.Lock()
        self._submitting = False
        self.result: Optional[SubmitResult] = None

    @property
    def submitted(self) -> bool:
        return self.result is not None

    def submit(self, answer: str, *, trace_id: Optional[str] = None) -> SubmitResult:
        """Submit the agent's final answer; it is graded before this returns.

        ``trace_id`` links the agent's own trace, if it sent one.
        """
        return self._submit(answer=answer, outcome="answered", error=None, trace_id=trace_id)

    def submit_error(
        self, error: Optional[str] = None, *, timeout: bool = False, trace_id: Optional[str] = None
    ) -> SubmitResult:
        """Record that the agent gave no answer: verdict ``error``, or ``timeout`` with ``timeout=True``."""
        return self._submit(answer=None, outcome="timeout" if timeout else "error", error=error, trace_id=trace_id)

    def _submit(
        self, *, answer: Optional[str], outcome: str, error: Optional[str], trace_id: Optional[str]
    ) -> SubmitResult:
        with self._lock:
            if self.result is not None or self._submitting:
                raise LayerLensError(f"attempt {self.attempt_id} ({self.task_name}) is already submitted")
            self._submitting = True
        try:
            run = self._run
            result = run._runs.submit(
                run.environment_id, run.run_id, self.attempt_id, answer, outcome=outcome, error=error, trace_id=trace_id
            )
            self.result = result
            return result
        finally:
            self._submitting = False


class ExternalRun:
    """A graded run of your own agent, from :meth:`Environments.external_run`.

    The run starts on entering ``with`` (or on :meth:`start`). Leaving the block
    normally finishes it, so attempts never submitted are graded ``error``; leaving it
    on an exception cancels it.
    """

    def __init__(
        self,
        runs: Runs,
        environment: str,
        *,
        agent_name: str,
        set_name: Optional[str],
        tasks: Optional[Sequence[TaskInput]],
        tasks_file: Union[str, Path, None],
        task_names: Optional[Sequence[str]],
        label: Optional[str],
        idempotency_key: IdempotencyKey,
    ) -> None:
        self._runs = runs
        self._environment = environment
        self._body: Dict[str, Any] = external_run_body(agent_name, set_name, tasks, tasks_file, task_names, label)
        self._idempotency_key = idempotency_key or new_idempotency_key()
        self._started: Optional[ExternalRunStart] = None
        self._environment_id: Optional[str] = None
        self._attempts: List[ExternalAttempt] = []
        self._cancel_error: Optional[LayerLensError] = None
        # What the cancel on leaving the block on an exception did (BUG-09), so a caller that
        # reports it, as `layerlens env run exec` does, need not cancel a second time.
        self._cancelled_run: Optional[RunResult] = None
        self._finished_before_cancel = False

    def start(self) -> ExternalRun:
        """Start the run, once; a repeated call returns the started run."""
        if self._started is None:
            environment_id = self._runs._environments.resolve(self._environment)
            started = self._runs._start_external(environment_id, self._body, self._idempotency_key)
            self._attempts = [ExternalAttempt(self, a) for a in started.attempts]
            self._environment_id = environment_id
            self._started = started
        return self

    @property
    def started(self) -> bool:
        return self._started is not None

    @property
    def cancel_error(self) -> Optional[LayerLensError]:
        """Why leaving the block on an exception could not cancel the run, when it could not."""
        return self._cancel_error

    @property
    def cancelled_run(self) -> Optional[RunResult]:
        """The run as leaving the block on an exception cancelled it; ``None`` when it did not."""
        return self._cancelled_run

    @property
    def finished_before_cancel(self) -> bool:
        """True when leaving the block on an exception found the run already finished.

        Every attempt had been graded, or the run was finished, before the cancel: there was
        nothing to cancel, so the API's 409 is not a failure and is not logged as one.
        """
        return self._finished_before_cancel

    @property
    def run_id(self) -> str:
        return _started(self._started).run_id

    @property
    def environment_id(self) -> str:
        _started(self._started)
        assert self._environment_id is not None
        return self._environment_id

    @property
    def attempts(self) -> List[ExternalAttempt]:
        _started(self._started)
        return list(self._attempts)

    def get(self) -> RunResult:
        return self._runs.get(self.environment_id, self.run_id)

    def finish(self) -> RunResult:
        """Close the run; attempts never submitted are graded ``error``. Safe to repeat."""
        return self._runs.finish(self.environment_id, self.run_id)

    def cancel(self) -> RunResult:
        return self._runs.cancel(self.environment_id, self.run_id)

    def wait(
        self,
        *,
        timeout_seconds: Optional[float] = DEFAULT_WAIT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> RunResult:
        """Block until the run has finished (every attempt graded, or :meth:`finish` called); returns it."""
        return self._runs.wait(
            self.environment_id,
            self.run_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def __enter__(self) -> ExternalRun:
        return self.start()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        if not self.started:
            return
        # This exit's outcome only: a run left twice must not report the first exit's cancel.
        self._cancelled_run, self._cancel_error, self._finished_before_cancel = None, None, False
        if exc_type is None:
            self.finish()
            return
        try:
            self._cancelled_run = self.cancel()
        except ConflictError:
            self._finished_before_cancel = True
            log.debug("run %s had finished before the cancel after %s", self.run_id, exc_type.__name__)
        except LayerLensError as err:
            self._cancel_error = err
            log.warning("could not cancel run %s after %s: %s", self.run_id, exc_type.__name__, err)


class AsyncExternalAttempt(_AttemptFields):
    """One task of a run of your own agent, on :class:`AsyncLayerLens`. See :class:`ExternalAttempt`."""

    def __init__(self, run: AsyncExternalRun, data: ExternalRunAttempt) -> None:
        self._run = run
        self._data = data
        self._submitting = False
        self.result: Optional[SubmitResult] = None

    @property
    def submitted(self) -> bool:
        return self.result is not None

    async def submit(self, answer: str, *, trace_id: Optional[str] = None) -> SubmitResult:
        return await self._submit(answer=answer, outcome="answered", error=None, trace_id=trace_id)

    async def submit_error(
        self, error: Optional[str] = None, *, timeout: bool = False, trace_id: Optional[str] = None
    ) -> SubmitResult:
        return await self._submit(
            answer=None, outcome="timeout" if timeout else "error", error=error, trace_id=trace_id
        )

    async def _submit(
        self, *, answer: Optional[str], outcome: str, error: Optional[str], trace_id: Optional[str]
    ) -> SubmitResult:
        if self.result is not None or self._submitting:
            raise LayerLensError(f"attempt {self.attempt_id} ({self.task_name}) is already submitted")
        self._submitting = True
        try:
            run = self._run
            result = await run._runs.submit(
                run.environment_id, run.run_id, self.attempt_id, answer, outcome=outcome, error=error, trace_id=trace_id
            )
            self.result = result
            return result
        finally:
            self._submitting = False


class AsyncExternalRun:
    """A graded run of your own agent on :class:`AsyncLayerLens`; use it with ``async with``. See :class:`ExternalRun`."""

    def __init__(
        self,
        runs: AsyncRuns,
        environment: str,
        *,
        agent_name: str,
        set_name: Optional[str],
        tasks: Optional[Sequence[TaskInput]],
        tasks_file: Union[str, Path, None],
        task_names: Optional[Sequence[str]],
        label: Optional[str],
        idempotency_key: IdempotencyKey,
    ) -> None:
        self._runs = runs
        self._environment = environment
        self._body: Dict[str, Any] = external_run_body(agent_name, set_name, tasks, tasks_file, task_names, label)
        self._idempotency_key = idempotency_key or new_idempotency_key()
        self._started: Optional[ExternalRunStart] = None
        self._environment_id: Optional[str] = None
        self._attempts: List[AsyncExternalAttempt] = []
        self._cancel_error: Optional[LayerLensError] = None
        # What the cancel on leaving the block on an exception did (BUG-09), so a caller that
        # reports it, as `layerlens env run exec` does, need not cancel a second time.
        self._cancelled_run: Optional[RunResult] = None
        self._finished_before_cancel = False

    async def start(self) -> AsyncExternalRun:
        if self._started is None:
            environment_id = await self._runs._environments.resolve(self._environment)
            started = await self._runs._start_external(environment_id, self._body, self._idempotency_key)
            self._attempts = [AsyncExternalAttempt(self, a) for a in started.attempts]
            self._environment_id = environment_id
            self._started = started
        return self

    @property
    def started(self) -> bool:
        return self._started is not None

    @property
    def cancel_error(self) -> Optional[LayerLensError]:
        """Why leaving the block on an exception could not cancel the run, when it could not."""
        return self._cancel_error

    @property
    def cancelled_run(self) -> Optional[RunResult]:
        """The run as leaving the block on an exception cancelled it; ``None`` when it did not."""
        return self._cancelled_run

    @property
    def finished_before_cancel(self) -> bool:
        """True when leaving the block on an exception found the run already finished.

        Every attempt had been graded, or the run was finished, before the cancel: there was
        nothing to cancel, so the API's 409 is not a failure and is not logged as one.
        """
        return self._finished_before_cancel

    @property
    def run_id(self) -> str:
        return _started(self._started).run_id

    @property
    def environment_id(self) -> str:
        _started(self._started)
        assert self._environment_id is not None
        return self._environment_id

    @property
    def attempts(self) -> List[AsyncExternalAttempt]:
        _started(self._started)
        return list(self._attempts)

    async def get(self) -> RunResult:
        return await self._runs.get(self.environment_id, self.run_id)

    async def finish(self) -> RunResult:
        return await self._runs.finish(self.environment_id, self.run_id)

    async def cancel(self) -> RunResult:
        return await self._runs.cancel(self.environment_id, self.run_id)

    async def wait(
        self,
        *,
        timeout_seconds: Optional[float] = DEFAULT_WAIT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> RunResult:
        return await self._runs.wait(
            self.environment_id,
            self.run_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    async def __aenter__(self) -> AsyncExternalRun:
        return await self.start()

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        if not self.started:
            return
        # This exit's outcome only: a run left twice must not report the first exit's cancel.
        self._cancelled_run, self._cancel_error, self._finished_before_cancel = None, None, False
        if exc_type is None:
            await self.finish()
            return
        try:
            self._cancelled_run = await self.cancel()
        except ConflictError:
            self._finished_before_cancel = True
            log.debug("run %s had finished before the cancel after %s", self.run_id, exc_type.__name__)
        except LayerLensError as err:
            self._cancel_error = err
            log.warning("could not cancel run %s after %s: %s", self.run_id, exc_type.__name__, err)


def _started(start: Optional[ExternalRunStart]) -> ExternalRunStart:
    if start is None:
        raise LayerLensError("this run has not started: enter its `with` block or call start() first")
    return start
