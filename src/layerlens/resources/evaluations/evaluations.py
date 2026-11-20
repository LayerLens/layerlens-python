from __future__ import annotations

import math
from typing import Any, Dict, List, Literal, Optional

import httpx

from ..._wire import json_object, parse_model
from ...models import (
    Model,
    Benchmark,
    Evaluation,
    CustomModel,
    CustomBenchmark,
    EvaluationStatus,
    EvaluationsResponse,
    CreateEvaluationsResponse,
)
from ..._polling import POLL_RETRIES, PollPolicy
from ..._resource import SyncAPIResource, AsyncAPIResource
from ..._constants import NOT_GIVEN, API_MAX_PAGE_SIZE, NotGiven
from ..._exceptions import LayerLensError, APIResponseValidationError

DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = API_MAX_PAGE_SIZE


def _parse_rows(payload: Dict[str, Any], *, response: httpx.Response, endpoint: str) -> List[Evaluation]:
    """Parse the `evaluations` array, naming the offending row if one fails.

    Each row is validated individually so the error identifies which of up to 100
    rows is at fault. A raw ``pydantic_core.ValidationError`` from a list
    comprehension names the field but not the row, and is not catchable as a
    ``layerlens`` exception type.

    A null array reads as no rows: a nil Go slice with no `omitempty` serializes as
    null, and iterating that raised a bare ``TypeError``. A body without an
    ``evaluations`` array is not a page of evaluations, so it raises
    ``APIResponseValidationError``: read as no rows, ``layerlens evaluate list`` printed
    "No evaluations found." and ``ci report --latest`` reported an empty project, both
    exiting 0. The API always sends the key.
    """
    if "evaluations" not in payload:
        raise APIResponseValidationError(
            response, payload, message=f"{endpoint} returned a 2xx response with no `evaluations` array"
        )
    rows = payload["evaluations"]
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise APIResponseValidationError(
            response,
            payload,
            message=f"{endpoint} returned a 2xx response whose `evaluations` is {type(rows).__name__}, not an array",
        )

    return [
        row
        if isinstance(row, Evaluation)
        else parse_model(
            Evaluation,
            row,
            response=response,
            endpoint=endpoint,
            detail=f"evaluations[{index}]",
        )
        for index, row in enumerate(rows)
    ]


def _wait_policy(evaluation: Evaluation, interval_seconds: float, timeout_seconds: Optional[float]) -> PollPolicy:
    # A timeout of 0 has always meant no limit here, as None does.
    return PollPolicy(
        f"Evaluation {evaluation.id}", interval_seconds=interval_seconds, timeout_seconds=timeout_seconds or None
    )


class Evaluations(SyncAPIResource):
    def create(
        self,
        *,
        model: Model,
        benchmark: Benchmark,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[Evaluation]:
        evaluations = self._post(
            f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/evaluations",
            body=[
                {
                    "model_id": model.id,
                    "dataset_id": benchmark.id,
                    "is_custom_model": isinstance(model, CustomModel),
                    "is_custom_dataset": isinstance(benchmark, CustomBenchmark),
                }
            ],
            timeout=timeout,
            cast_to=CreateEvaluationsResponse,
        )
        if isinstance(evaluations, CreateEvaluationsResponse) and len(evaluations.data) > 0:
            evaluation = evaluations.data[0]
            evaluation.attach_client(self._client)
            return evaluation
        return None

    def get(
        self,
        evaluation: Evaluation,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[Evaluation]:
        """The evaluation read again by its id (see :meth:`get_by_id`)."""
        return self.get_by_id(evaluation.id, timeout=timeout)

    def get_by_id(
        self,
        id: str,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[Evaluation]:
        """An evaluation, by id. Raises :class:`NotFoundError` when no evaluation has this id."""
        return self._read(id, timeout=timeout)

    def _read(
        self, id: str, *, timeout: float | httpx.Timeout | None | NotGiven, **request: Any
    ) -> Optional[Evaluation]:
        evaluation = self._get(f"/evaluations/{id}", timeout=timeout, cast_to=Evaluation, **request)
        if isinstance(evaluation, Evaluation):
            evaluation.attach_client(self._client)
            return evaluation
        return None

    def get_many(
        self,
        *,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        sort_by: Optional[Literal["submitted_at", "accuracy", "average_duration"]] = None,
        order: Optional[Literal["asc", "desc"]] = None,
        model_ids: Optional[List[str]] = None,
        benchmark_ids: Optional[List[str]] = None,
        status: Optional[EvaluationStatus] = None,
        unique: bool = False,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[EvaluationsResponse]:
        """
        Get evaluations with optional pagination, sorting, and filtering.

        Args:
            page: Page number for pagination (1-based, defaults to 1 if not provided)
            page_size: Number of evaluations per page (default: 100, optional)
            sort_by: Sort evaluations by field (submitted_at, accuracy, average_duration)
            order: Sort order (asc or desc)
            model_ids: Filter by model IDs
            benchmark_ids: Filter by benchmark/dataset IDs
            status: Filter by evaluation status
            unique: If True, deduplicate by model+dataset keeping only the latest evaluation per pair
            timeout: Request timeout

        Returns:
            EvaluationsResponse object or None
        """
        params = {
            "organization_id": self._client.organization_id,
            "project_id": self._client.project_id,
        }

        effective_page_size = min(max(page_size, 1), MAX_PAGE_SIZE) if page_size is not None else DEFAULT_PAGE_SIZE
        effective_page = page if page is not None else DEFAULT_PAGE

        params["page"] = str(effective_page)
        params["page_size"] = str(effective_page_size)

        if sort_by:
            params["sort_by"] = sort_by
        if order:
            params["order"] = order
        if model_ids:
            params["models"] = ",".join(model_ids)
        if benchmark_ids:
            params["datasets"] = ",".join(benchmark_ids)
        if status:
            params["status"] = status.value
        if unique:
            params["unique"] = "true"

        # The raw response, not cast_to=dict: holding it is what lets a bad row
        # below raise the SDK's typed error with the payload and field path
        # attached, instead of a raw pydantic_core.ValidationError.
        response = self._get(f"/evaluations", params=params, timeout=timeout)
        assert isinstance(response, httpx.Response), (
            "expected the raw response: this call passes no cast_to, so the transport must hand back an httpx.Response"
        )

        payload = json_object(response, endpoint="/evaluations")
        evaluations = _parse_rows(payload, response=response, endpoint="/evaluations")
        for e in evaluations:
            e.attach_client(self._client)

        total_count = payload.get("total_count", 0)
        total_pages = math.ceil(total_count / effective_page_size) if total_count > 0 and effective_page_size > 0 else 0

        resp_with_pagination = {
            "evaluations": evaluations,
            "pagination": {
                "page": effective_page,
                "page_size": effective_page_size,
                "total_pages": total_pages,
                "total_count": total_count,
            },
        }

        return parse_model(
            EvaluationsResponse,
            resp_with_pagination,
            response=response,
            endpoint="/evaluations",
            detail=f"page {effective_page}",
        )

    def wait_for_completion(
        self,
        evaluation: Evaluation,
        *,
        interval_seconds: int = 30,
        timeout_seconds: Optional[int] = None,
    ) -> Optional[Evaluation]:
        """Poll every ``interval_seconds`` until the evaluation finishes, and return it.

        A poll that fails on a 429, a 5xx or a lost connection does not end the wait: the
        next poll follows half a second later, then after twice as long each time it fails
        again, up to ``interval_seconds``, and never sooner than the reply's ``Retry-After``.
        Each poll is sent once, without the client's own retries. Any other error is raised:
        :class:`NotFoundError` when no evaluation has this id any more (a 404). Returns ``None`` only
        for a reply that holds no evaluation. Raises :class:`TimeoutError` when it has not
        finished after ``timeout_seconds`` (``None`` or ``0`` waits as long as it takes).

        Each request is sent once with the client's timeout cut to the time left (httpx applies it
        to connecting and to each wait for data), and none is sent once ``timeout_seconds`` has
        passed. The wait aims a poll at 2 seconds before the deadline, so that poll has time to
        answer; polls can also run inside those 2 seconds, none with less than half a second left. A
        wait never polls sooner than a ``Retry-After`` asks. When the ``Retry-After`` reaches past
        the wait's deadline, the wait sleeps until the deadline and then raises, with the 429 as the
        cause. A wait with no deadline (``timeout_seconds`` of ``None`` or ``0``) reads a
        ``Retry-After`` longer than 5 minutes as 5 minutes. A client's first organization read, made
        once per client, comes before the wait and is not counted in ``timeout_seconds``.
        """
        policy = _wait_policy(evaluation, interval_seconds, timeout_seconds)
        while True:
            try:
                current = self._read(
                    evaluation.id, timeout=policy.poll_timeout(self._client.timeout), max_retries=POLL_RETRIES
                )
            except LayerLensError as err:
                delay = policy.after_error(err)
            else:
                if current is None or current.is_finished:
                    return current
                delay = policy.after_pending()
            self._sleep(delay)


class AsyncEvaluations(AsyncAPIResource):
    async def create(
        self,
        *,
        model: Model,
        benchmark: Benchmark,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[Evaluation]:
        evaluations = await self._post(
            f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/evaluations",
            body=[
                {
                    "model_id": model.id,
                    "dataset_id": benchmark.id,
                    "is_custom_model": isinstance(model, CustomModel),
                    "is_custom_dataset": isinstance(benchmark, CustomBenchmark),
                }
            ],
            timeout=timeout,
            cast_to=CreateEvaluationsResponse,
        )
        if isinstance(evaluations, CreateEvaluationsResponse) and len(evaluations.data) > 0:
            evaluation = evaluations.data[0]
            evaluation.attach_client(self._client)
            return evaluation
        return None

    async def get(
        self,
        evaluation: Evaluation,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[Evaluation]:
        """The evaluation read again by its id (see :meth:`get_by_id`)."""
        return await self.get_by_id(evaluation.id, timeout=timeout)

    async def get_by_id(
        self,
        id: str,
        *,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[Evaluation]:
        """An evaluation, by id. Raises :class:`NotFoundError` when no evaluation has this id."""
        return await self._read(id, timeout=timeout)

    async def _read(
        self, id: str, *, timeout: float | httpx.Timeout | None | NotGiven, **request: Any
    ) -> Optional[Evaluation]:
        evaluation = await self._get(f"/evaluations/{id}", timeout=timeout, cast_to=Evaluation, **request)
        if isinstance(evaluation, Evaluation):
            evaluation.attach_client(self._client)
            return evaluation
        return None

    async def get_many(
        self,
        *,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        sort_by: Optional[Literal["submitted_at", "accuracy", "average_duration"]] = None,
        order: Optional[Literal["asc", "desc"]] = None,
        model_ids: Optional[List[str]] = None,
        benchmark_ids: Optional[List[str]] = None,
        status: Optional[EvaluationStatus] = None,
        unique: bool = False,
        timeout: float | httpx.Timeout | None | NotGiven = NOT_GIVEN,
    ) -> Optional[EvaluationsResponse]:
        """
        Get evaluations with optional pagination, sorting, and filtering.

        Args:
            page: Page number for pagination (1-based, defaults to 1 if not provided)
            page_size: Number of evaluations per page (default: 100, optional)
            sort_by: Sort evaluations by field (submitted_at, accuracy, average_duration)
            order: Sort order (asc or desc)
            model_ids: Filter by model IDs
            benchmark_ids: Filter by benchmark/dataset IDs
            status: Filter by evaluation status
            unique: If True, deduplicate by model+dataset keeping only the latest evaluation per pair
            timeout: Request timeout

        Returns:
            EvaluationsResponse object or None
        """
        params = {
            "organization_id": self._client.organization_id,
            "project_id": self._client.project_id,
        }

        effective_page_size = min(max(page_size, 1), MAX_PAGE_SIZE) if page_size is not None else DEFAULT_PAGE_SIZE
        effective_page = page if page is not None else DEFAULT_PAGE

        params["page"] = str(effective_page)
        params["page_size"] = str(effective_page_size)

        if sort_by:
            params["sort_by"] = sort_by
        if order:
            params["order"] = order
        if model_ids:
            params["models"] = ",".join(model_ids)
        if benchmark_ids:
            params["datasets"] = ",".join(benchmark_ids)
        if status:
            params["status"] = status.value
        if unique:
            params["unique"] = "true"

        # See the sync twin.
        response = await self._get(f"/evaluations", params=params, timeout=timeout)
        assert isinstance(response, httpx.Response), (
            "expected the raw response: this call passes no cast_to, so the transport must hand back an httpx.Response"
        )

        payload = json_object(response, endpoint="/evaluations")
        evaluations = _parse_rows(payload, response=response, endpoint="/evaluations")
        for e in evaluations:
            e.attach_client(self._client)

        total_count = payload.get("total_count", 0)
        total_pages = math.ceil(total_count / effective_page_size) if total_count > 0 and effective_page_size > 0 else 0

        resp_with_pagination = {
            "evaluations": evaluations,
            "pagination": {
                "page": effective_page,
                "page_size": effective_page_size,
                "total_pages": total_pages,
                "total_count": total_count,
            },
        }

        return parse_model(
            EvaluationsResponse,
            resp_with_pagination,
            response=response,
            endpoint="/evaluations",
            detail=f"page {effective_page}",
        )

    async def wait_for_completion(
        self,
        evaluation: Evaluation,
        *,
        interval_seconds: int = 30,
        timeout_seconds: Optional[int] = None,
    ) -> Optional[Evaluation]:
        """Poll every ``interval_seconds`` until the evaluation finishes, and return it.

        See :meth:`Evaluations.wait_for_completion`.
        """
        policy = _wait_policy(evaluation, interval_seconds, timeout_seconds)
        while True:
            try:
                current = await self._read(
                    evaluation.id, timeout=policy.poll_timeout(self._client.timeout), max_retries=POLL_RETRIES
                )
            except LayerLensError as err:
                delay = policy.after_error(err)
            else:
                if current is None or current.is_finished:
                    return current
                delay = policy.after_pending()
            await self._sleep(delay)
