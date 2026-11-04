"""Transport and request helpers shared by the sync and async environments resources."""

from __future__ import annotations

import re
import json
import uuid
from typing import Any, Dict, List, Type, Union, Mapping, TypeVar, Optional, Sequence, NamedTuple
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError

from ..._utils import is_mapping
from ..._polling import Deadline, PollPolicy, retry_after_seconds
from ..._exceptions import (
    ConflictError,
    APIStatusError,
    LayerLensError,
    RateLimitError,
    APIConnectionError,
    InternalServerError,
    EnvironmentConflictError,
    APIResponseValidationError,
)
from ..._base_client import MAX_RETRY_DELAY, RETRY_STATUS_CODES, INITIAL_RETRY_DELAY
from ...models.environment import TaskSpec, RunResult, RunAttempt, SubmitResult

ModelT = TypeVar("ModelT", bound=BaseModel)

# Sets of at most 100 tasks per page, and environment lists of at most 500, are what the API serves.
TASK_SET_PAGE_SIZE = 100
ENVIRONMENT_PAGE_SIZE = 500

DEFAULT_WAIT_SECONDS = 1800.0
DEFAULT_POLL_SECONDS = 5.0
MAX_POLL_SECONDS = 15.0

# A submit whose answer may have reached the API is settled within this long: the API
# frees a stuck claim on an answer after 5 minutes, and grading itself takes seconds.
SUBMIT_SETTLE_SECONDS = 420.0
SUBMIT_RETRY_SECONDS = 3.0

SUBMIT_OUTCOMES = ("answered", "error", "timeout")

# The API refuses a larger request body with 413; one over Lambda's 6 MB never reaches it,
# and comes back as a bare gateway error instead.
MAX_REQUEST_BODY_BYTES = 5 << 20

# The gateway hands the body to Lambda as a JSON string inside its event, which holds at most
# 6 MiB; this leaves 256 KiB of it for the headers and the request context.
MAX_ESCAPED_BODY_BYTES = (6 << 20) - (256 << 10)

# How long, in all, a request that carries an Idempotency-Key keeps waiting out a 503 that
# came with Retry-After.
BUSY_RETRY_SECONDS = 30.0

_OBJECT_ID = re.compile(r"[0-9a-f]{24}")


class InvalidArgument(ValueError):
    """An argument the SDK refuses before sending anything, such as two sources of tasks."""


IdempotencyKey = Optional[str]


def check_body_size(body: Any) -> None:
    """Refuse a body the API would not take, before sending it: by its own size, or by its size
    once the gateway escapes it into Lambda's event (non-ASCII text is not escaped there)."""
    content = httpx.Request("POST", "https://api.invalid", json=body).content
    size = len(content)
    escaped = len(json.dumps(content.decode("utf-8"), ensure_ascii=False).encode("utf-8"))
    if size > MAX_REQUEST_BODY_BYTES or escaped > MAX_ESCAPED_BODY_BYTES:
        raise InvalidArgument(
            f"this request is {_mib(size)} ({size} bytes) of JSON, {_mib(escaped)} ({escaped} bytes) once the "
            "API's gateway escapes it, where every quote and backslash counts twice, and the API takes at most "
            f"{_mib(MAX_REQUEST_BODY_BYTES)} ({MAX_REQUEST_BODY_BYTES} bytes), or {_mib(MAX_ESCAPED_BODY_BYTES)} "
            f"({MAX_ESCAPED_BODY_BYTES} bytes) escaped: run fewer or shorter inline tasks, or store the tasks in "
            "a task set and run the set"
        )


def _mib(size: int) -> str:
    return f"{size / (1 << 20):.2f} MiB"


def is_object_id(value: str) -> bool:
    return _OBJECT_ID.fullmatch(value) is not None


def new_idempotency_key() -> str:
    return f"layerlens-sdk-{uuid.uuid4().hex}"


def environments_path(organization_id: Optional[str], project_id: Optional[str]) -> str:
    return f"/env/organizations/{organization_id}/projects/{project_id}/environments"


def require_api_key_client(use_bearer_auth: bool) -> None:
    if use_bearer_auth:
        raise LayerLensError(
            "client.environments needs an API key with Automation access (Organization Settings → API Keys); "
            "a `layerlens login` session cannot reach the Automation API"
        )


def unwrap(response: httpx.Response, model: Type[ModelT], *, key: Optional[str] = None) -> ModelT:
    """The ``data`` of a ``{"status": "success", "data": …}`` reply, as ``model``.

    Raises :class:`APIResponseValidationError` when the reply is not that envelope or
    ``data`` does not fit ``model``, never ``None``: an unreadable reply is a failure.
    """
    data = envelope_data(response)
    if key is not None:
        if not is_mapping(data) or key not in data:
            raise APIResponseValidationError(response, data, message=f"the reply has no {key!r}")
        data = data[key]
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise APIResponseValidationError(response, data, message=f"the reply does not fit {model.__name__}") from err


def unwrap_list(response: httpx.Response, model: Type[ModelT], key: str) -> List[ModelT]:
    data = envelope_data(response)
    items = data.get(key) if is_mapping(data) else None
    if not isinstance(items, list):
        raise APIResponseValidationError(response, data, message=f"the reply has no list {key!r}")
    try:
        return [model.model_validate(item) for item in items]
    except ValidationError as err:
        raise APIResponseValidationError(response, data, message=f"the reply does not fit {model.__name__}") from err


def envelope_data(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError as err:
        raise APIResponseValidationError(response, response.text, message="the reply is not JSON") from err
    if not is_mapping(body) or body.get("status") != "success" or "data" not in body:
        raise APIResponseValidationError(response, body, message="the reply is not a success envelope")
    return body["data"]


def as_response(value: object) -> httpx.Response:
    if not isinstance(value, httpx.Response):
        raise TypeError(f"expected an httpx.Response, got {type(value).__name__}")
    return value


def error_body(response: httpx.Response) -> Dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return dict(body) if is_mapping(body) else {}


def environment_conflict(err: ConflictError) -> EnvironmentConflictError:
    body = error_body(err.response)
    raw = body.get("differences")
    differences = [str(d) for d in raw] if isinstance(raw, list) else []
    project_id = body.get("project_id")
    message = body.get("error") or body.get("message")
    return EnvironmentConflictError(
        str(message) if isinstance(message, str) and message else err.message,
        response=err.response,
        body=err.body,
        differences=differences,
        project_id=str(project_id) if isinstance(project_id, str) and project_id else None,
    )


def create_body(
    *,
    name: str,
    type: str,
    description: str,
    config: Optional[Mapping[str, Any]],
    seed: Optional[int],
    if_not_exists: bool,
) -> Dict[str, Any]:
    if not description or not description.strip():
        raise InvalidArgument("description: describe the environment in words; every environment starts from one")
    body: Dict[str, Any] = {"name": name, "type": type, "description": description}
    if config is not None:
        body["config"] = dict(config)
    if seed is not None:
        body["seed"] = seed
    if if_not_exists:
        body["if_not_exists"] = True
    return body


TaskInput = Union[TaskSpec, Mapping[str, Any]]


def load_tasks_file(path: Union[str, Path]) -> List[Dict[str, Any]]:
    """The tasks of a JSONL file: one JSON object per line, blank lines skipped."""
    tasks: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError as err:
        raise InvalidArgument(f"{path} is not UTF-8 text: {err}") from err
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            task = json.loads(line)
        except ValueError as err:
            raise InvalidArgument(f"{path}:{number}: not a JSON object: {err}") from err
        if not isinstance(task, dict):
            raise InvalidArgument(f"{path}:{number}: each line must be one JSON object, one task")
        tasks.append(task)
    if not tasks:
        raise InvalidArgument(f"{path} holds no tasks")
    return tasks


def task_choice(
    *,
    set_name: Optional[str],
    tasks: Optional[Sequence[TaskInput]],
    tasks_file: Union[str, Path, None],
    task_names: Optional[Sequence[str]],
) -> Dict[str, Any]:
    """The task fields of a run's body: exactly one of a stored set, inline tasks or a tasks file."""
    given = [n for n, v in (("set_name", set_name), ("tasks", tasks), ("tasks_file", tasks_file)) if v is not None]
    if len(given) != 1:
        raise InvalidArgument(
            "choose the run's tasks with exactly one of set_name, tasks or tasks_file"
            + (f" (got {', '.join(given)})" if given else "")
        )
    body: Dict[str, Any] = {}
    if set_name is not None:
        body["set_name"] = set_name
    elif tasks_file is not None:
        body["tasks"] = load_tasks_file(tasks_file)
    else:
        assert tasks is not None
        body["tasks"] = [t.model_dump() if isinstance(t, TaskSpec) else dict(t) for t in tasks]
    if task_names is not None:
        if isinstance(task_names, str):
            raise TypeError("task_names is a list of task names, not one string")
        body["task_names"] = list(task_names)
    return body


def submit_body(
    *, answer: Optional[str], outcome: str, error: Optional[str], trace_id: Optional[str]
) -> Dict[str, Any]:
    if outcome not in SUBMIT_OUTCOMES:
        raise InvalidArgument(f"outcome must be one of {', '.join(SUBMIT_OUTCOMES)}, got {outcome!r}")
    body: Dict[str, Any] = {"outcome": outcome}
    if answer is not None:
        body["answer"] = answer
    if error is not None:
        body["error"] = error
    if trace_id is not None:
        body["trace_id"] = trace_id
    return body


class SubmitState(NamedTuple):
    # The attempt is graded; ``result`` says so as a submit would have.
    graded: bool
    # The run ended without grading the attempt.
    closed: bool
    result: Optional[SubmitResult]


def _stored(row: RunAttempt, finished: bool) -> bool:
    """Whether a row is an attempt that was stored, rather than a task listed as never submitted.

    A finished run lists the tasks never submitted as verdict ``error``. Such a row carries
    none of a stored attempt's own fields (answer, trace, cost), which tells it from an
    attempt graded ``error``. A stored attempt that has none of them reads as not stored,
    so a doubt is raised rather than reported as a grade.
    """
    if row.verdict is None:
        return False
    return not finished or any(
        v is not None for v in (row.agent_answer, row.trace_id, row.cost_usd, row.agent_trace_id)
    )


def submit_state(run: RunResult, attempt_id: str) -> SubmitState:
    """Where an attempt's submit stands, read from its run."""
    row = next((a for a in run.attempts if a.attempt_id == attempt_id), None)
    if row is None:
        raise LayerLensError(f"run {run.run_id} has no attempt {attempt_id}")
    if _stored(row, run.finished):
        result = SubmitResult(
            run_id=run.run_id,
            task_id=row.task_id,
            attempt_id=attempt_id,
            answered=sum(1 for a in run.attempts if _stored(a, run.finished)),
            total=run.total,
            finished=run.finished,
        )
        return SubmitState(graded=True, closed=False, result=result)
    return SubmitState(graded=False, closed=run.finished, result=None)


# The API's refusals of an answer that is held back for a while, not for good: a call of
# the attempt still being served, or another answer to it being graded (the claim is
# freed when that request ends, or 5 minutes after it was taken).
_HELD_BACK = ("is still being served", "is already being graded")

# Resubmits after the API failed on an answer; each may grade it again (a judge call).
SUBMIT_RETRIES_AFTER_FAILURE = 3


class SubmitPolicy:
    """What a submit does after each failed request; the same for the sync and async clients.

    A request that failed on the server's side, or whose reply never came, may still have
    been graded, so the run is read to settle it. An answer held back by a claim is sent
    again until the claim is freed. Every other refusal is raised at once.
    """

    def __init__(self, run_id: str, attempt_id: str) -> None:
        self._run_id = run_id
        self._attempt_id = attempt_id
        self._deadline = Deadline(SUBMIT_SETTLE_SECONDS)
        self._interval = SUBMIT_RETRY_SECONDS
        self._landed: Optional[LayerLensError] = None
        self._refused: Optional[LayerLensError] = None
        self._held_back = False
        self._failures = 0

    def after_error(self, err: LayerLensError) -> Optional[float]:
        """Raise ``err`` if the submit is over; else the seconds to wait before sending it
        again, or ``None`` to read the run first."""
        if isinstance(err, RateLimitError):
            if self._deadline.passed():
                raise err
            return self._deadline.clip(retry_after_seconds(err, SUBMIT_RETRY_SECONDS))
        if isinstance(err, ConflictError):
            self._refused = err
            self._held_back = any(words in err.message or words in str(err.body) for words in _HELD_BACK)
            if self._landed is None and not self._held_back:
                raise err
            return None
        if isinstance(err, InternalServerError) and err.status_code == 503 and err.response.headers.get("retry-after"):
            # Refused before any handler ran (a throttled API key lookup): nothing was graded.
            if self._deadline.passed():
                raise err
            return self._deadline.clip(retry_after_seconds(err, SUBMIT_RETRY_SECONDS))
        if isinstance(err, InternalServerError) or (isinstance(err, APIConnectionError) and not _never_sent(err)):
            self._landed = self._refused = err
            self._failures += 1
            return None
        if isinstance(err, APIConnectionError):
            self._refused = err
            if self._deadline.passed():
                raise err
            return self.pause()
        raise err

    def after_state(self, state: Optional[SubmitState]) -> Optional[SubmitResult]:
        """The result once the run shows the attempt graded; raise when it never will be;
        ``None`` to wait and go on."""
        refused = self._refused
        assert refused is not None
        if state is not None and state.graded:
            if self._landed is None or state.result is None:
                raise refused
            return state.result
        if state is not None and state.closed:
            if self._landed is None:
                raise refused
            raise LayerLensError(f"run {self._run_id} ended before attempt {self._attempt_id} was graded") from (
                self._landed
            )
        if isinstance(refused, ConflictError) and not self._held_back and state is not None:
            raise refused
        if self._deadline.passed():
            raise refused
        return None

    @property
    def send_again(self) -> bool:
        """Whether to send the answer again, or only read the run until it settles."""
        return self._failures <= SUBMIT_RETRIES_AFTER_FAILURE

    def pause(self) -> float:
        seconds = self._deadline.clip(self._interval)
        self._interval = next_poll(self._interval)
        return seconds


class ResendPolicy:
    """When a request that carries an Idempotency-Key is sent again; the same for the sync and async clients.

    The API answers a key it has seen with what the first request started, so a request
    whose reply never came (a timeout, a reset connection) is sent again under the same key,
    as often as the client retries. A 503 with ``Retry-After`` is the API refusing before it
    started anything, as when too many jobs are being started with the organization's keys
    at once: it is sent again when the header says, for up to ``BUSY_RETRY_SECONDS`` in all.
    Other 429 and 5xx replies get the client's usual retries; every other error is raised.
    """

    def __init__(self, max_retries: int) -> None:
        self._retries_left = max_retries
        self._busy_seconds = 0.0
        self._delay = INITIAL_RETRY_DELAY

    def after_error(self, err: LayerLensError) -> float:
        """The seconds to wait before sending the request again; raises ``err`` when it is over."""
        if isinstance(err, APIStatusError):
            if err.status_code not in RETRY_STATUS_CODES:
                raise err
            if err.status_code == 503 and err.response.headers.get("retry-after"):
                return self._busy(err)
        elif not isinstance(err, APIConnectionError):
            raise err
        if self._retries_left <= 0:
            raise err
        self._retries_left -= 1
        seconds = min(retry_after_seconds(err, self._delay), MAX_RETRY_DELAY)
        self._delay = min(self._delay * 2, MAX_RETRY_DELAY)
        return seconds

    def _busy(self, err: APIStatusError) -> float:
        left = BUSY_RETRY_SECONDS - self._busy_seconds
        if left <= 0 or self._retries_left <= 0:
            raise err
        seconds = min(max(retry_after_seconds(err, INITIAL_RETRY_DELAY), INITIAL_RETRY_DELAY), left)
        self._busy_seconds += seconds
        return seconds


def _never_sent(err: APIConnectionError) -> bool:
    return isinstance(err.__cause__, (httpx.ConnectError, httpx.ConnectTimeout))


def next_poll(interval: float) -> float:
    return min(interval * 1.5, MAX_POLL_SECONDS)


def wait_policy(what: str, timeout_seconds: Optional[float], poll_interval_seconds: float) -> PollPolicy:
    """How an environments wait polls: from ``poll_interval_seconds``, half as long again
    after each poll, up to ``MAX_POLL_SECONDS``."""
    return PollPolicy(
        what,
        interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
        max_interval_seconds=MAX_POLL_SECONDS,
    )


def still(state: str, timeout_seconds: Optional[float]) -> str:
    """A wait's timeout message: ``run 6a91… is still in_progress after 1800s``."""
    return state if timeout_seconds is None else f"{state} after {timeout_seconds:g}s"
