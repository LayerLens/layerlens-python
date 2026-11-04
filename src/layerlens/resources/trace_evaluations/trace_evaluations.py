from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from ...models import (
    TraceEvaluation,
    CostEstimateResponse,
    TraceEvaluationStatus,
    TraceEvaluationsResponse,
    TraceEvaluationResultsResponse,
)
from ..._polling import POLL_RETRIES, PollPolicy
from ..._resource import SyncAPIResource, AsyncAPIResource
from ..._constants import DEFAULT_TIMEOUT
from ..._exceptions import NotFoundError, LayerLensError

_TERMINAL_STATUSES = {
    TraceEvaluationStatus.SUCCESS,
    TraceEvaluationStatus.FAILURE,
}

DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _unwrap(resp: Any) -> Any:
    """Unwrap {"status": ..., "data": ...} envelope if present."""
    if isinstance(resp, dict) and "data" in resp and "status" in resp:
        return resp["data"]
    return resp


def _finished(te: Optional[TraceEvaluation]) -> bool:
    return te is not None and te.status in _TERMINAL_STATUSES


def _wait_policy(id: str, interval_seconds: float, timeout_seconds: Optional[float]) -> PollPolicy:
    # A timeout of 0 has always meant no limit here, as None does.
    return PollPolicy(
        f"Trace evaluation {id}", interval_seconds=interval_seconds, timeout_seconds=timeout_seconds or None
    )


class TraceEvaluations(SyncAPIResource):
    def _base_url(self) -> str:
        return f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/trace-evaluations"

    def create(
        self,
        *,
        trace_id: str,
        judge_id: str,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Optional[TraceEvaluation]:
        resp = self._post(
            self._base_url(),
            body={"trace_id": trace_id, "judge_id": judge_id},
            timeout=timeout,
            cast_to=dict,
        )
        data = _unwrap(resp)
        if isinstance(data, dict):
            try:
                return TraceEvaluation(**data)
            except Exception:
                return None
        return None

    def get(
        self,
        id: str,
        *,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Optional[TraceEvaluation]:
        return self._read(id, timeout=timeout)

    def _read(self, id: str, *, timeout: float | httpx.Timeout | None, **request: Any) -> Optional[TraceEvaluation]:
        resp = self._get(f"{self._base_url()}/{id}", timeout=timeout, cast_to=dict, **request)
        data = _unwrap(resp)
        if isinstance(data, dict):
            try:
                return TraceEvaluation(**data)
            except Exception:
                return None
        return None

    def get_many(
        self,
        *,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        judge_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        outcome: Optional[str] = None,
        time_range: Optional[str] = None,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Optional[TraceEvaluationsResponse]:
        params: Dict[str, Any] = {}

        effective_page_size = min(max(page_size, 1), MAX_PAGE_SIZE) if page_size is not None else DEFAULT_PAGE_SIZE
        effective_page = page if page is not None else DEFAULT_PAGE

        params["page"] = str(effective_page)
        params["page_size"] = str(effective_page_size)

        if judge_id is not None:
            params["judge_id"] = judge_id
        if trace_id is not None:
            params["trace_id"] = trace_id
        if outcome is not None:
            params["outcome"] = outcome
        if time_range is not None:
            params["time_range"] = time_range
        if search is not None:
            params["search"] = search
        if sort_by is not None:
            params["sort_by"] = sort_by
        if sort_order is not None:
            params["sort_order"] = sort_order

        resp = self._get(
            self._base_url(),
            params=params,
            timeout=timeout,
            cast_to=dict,
        )
        if not resp or not isinstance(resp, dict):
            return None

        data = _unwrap(resp)
        if not isinstance(data, dict):
            return None

        evaluations = [
            te if isinstance(te, TraceEvaluation) else TraceEvaluation(**te) for te in data.get("trace_evaluations", [])
        ]
        count: int = data.get("count", len(evaluations))
        total: int = data.get("total", count)

        try:
            return TraceEvaluationsResponse(trace_evaluations=evaluations, count=count, total=total)
        except Exception:
            return None

    def get_results(
        self,
        id: str,
        *,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Optional[TraceEvaluationResultsResponse]:
        return self._read_results(id, timeout=timeout)

    def _read_results(
        self, id: str, *, timeout: float | httpx.Timeout | None, **request: Any
    ) -> Optional[TraceEvaluationResultsResponse]:
        try:
            resp = self._get(f"{self._base_url()}/{id}/results", timeout=timeout, cast_to=dict, **request)
        except NotFoundError:
            return None
        data = _unwrap(resp)
        if not data or not isinstance(data, dict):
            return None

        try:
            return TraceEvaluationResultsResponse(**data)
        except Exception:
            return None

    def wait_for_completion(
        self,
        id: str,
        *,
        interval_seconds: int = 3,
        timeout_seconds: Optional[int] = 300,
    ) -> Optional[TraceEvaluationResultsResponse]:
        """Poll every ``interval_seconds`` until the trace evaluation finishes, then return its results.

        A poll, or the read of the results, that fails on a 429, a 5xx or a lost connection
        does not end the wait: it is tried again half a second later, then after twice as
        long each time it fails again, up to ``interval_seconds``, and never sooner than the
        reply's ``Retry-After``. Each request is sent once, without the client's own
        retries. Any other error is raised. Returns ``None`` when the
        evaluation failed and has no results. Raises :class:`TimeoutError` when there are
        no results after ``timeout_seconds`` (``None`` waits as long as it takes).
        """
        policy = _wait_policy(id, interval_seconds, timeout_seconds)
        finished = False
        while True:
            try:
                if not finished:
                    finished = _finished(self._read(id, timeout=DEFAULT_TIMEOUT, max_retries=POLL_RETRIES))
                if finished:
                    return self._read_results(id, timeout=DEFAULT_TIMEOUT, max_retries=POLL_RETRIES)
            except LayerLensError as err:
                delay = policy.after_error(err)
            else:
                delay = policy.after_pending()
            self._sleep(delay)

    def estimate_cost(
        self,
        *,
        trace_ids: List[str],
        judge_id: str,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Optional[CostEstimateResponse]:
        resp = self._post(
            f"{self._base_url()}/estimate",
            body={"trace_ids": trace_ids, "judge_id": judge_id},
            timeout=timeout,
            cast_to=dict,
        )
        data = _unwrap(resp)
        if isinstance(data, dict):
            try:
                return CostEstimateResponse(**data)
            except Exception:
                return None
        return None


class AsyncTraceEvaluations(AsyncAPIResource):
    def _base_url(self) -> str:
        return f"/organizations/{self._client.organization_id}/projects/{self._client.project_id}/trace-evaluations"

    async def create(
        self,
        *,
        trace_id: str,
        judge_id: str,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Optional[TraceEvaluation]:
        resp = await self._post(
            self._base_url(),
            body={"trace_id": trace_id, "judge_id": judge_id},
            timeout=timeout,
            cast_to=dict,
        )
        data = _unwrap(resp)
        if isinstance(data, dict):
            try:
                return TraceEvaluation(**data)
            except Exception:
                return None
        return None

    async def get(
        self,
        id: str,
        *,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Optional[TraceEvaluation]:
        return await self._read(id, timeout=timeout)

    async def _read(
        self, id: str, *, timeout: float | httpx.Timeout | None, **request: Any
    ) -> Optional[TraceEvaluation]:
        resp = await self._get(f"{self._base_url()}/{id}", timeout=timeout, cast_to=dict, **request)
        data = _unwrap(resp)
        if isinstance(data, dict):
            try:
                return TraceEvaluation(**data)
            except Exception:
                return None
        return None

    async def get_many(
        self,
        *,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        judge_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        outcome: Optional[str] = None,
        time_range: Optional[str] = None,
        search: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Optional[TraceEvaluationsResponse]:
        params: Dict[str, Any] = {}

        effective_page_size = min(max(page_size, 1), MAX_PAGE_SIZE) if page_size is not None else DEFAULT_PAGE_SIZE
        effective_page = page if page is not None else DEFAULT_PAGE

        params["page"] = str(effective_page)
        params["page_size"] = str(effective_page_size)

        if judge_id is not None:
            params["judge_id"] = judge_id
        if trace_id is not None:
            params["trace_id"] = trace_id
        if outcome is not None:
            params["outcome"] = outcome
        if time_range is not None:
            params["time_range"] = time_range
        if search is not None:
            params["search"] = search
        if sort_by is not None:
            params["sort_by"] = sort_by
        if sort_order is not None:
            params["sort_order"] = sort_order

        resp = await self._get(
            self._base_url(),
            params=params,
            timeout=timeout,
            cast_to=dict,
        )
        if not resp or not isinstance(resp, dict):
            return None

        data = _unwrap(resp)
        if not isinstance(data, dict):
            return None

        evaluations = [
            te if isinstance(te, TraceEvaluation) else TraceEvaluation(**te) for te in data.get("trace_evaluations", [])
        ]
        count: int = data.get("count", len(evaluations))
        total: int = data.get("total", count)

        try:
            return TraceEvaluationsResponse(trace_evaluations=evaluations, count=count, total=total)
        except Exception:
            return None

    async def get_results(
        self,
        id: str,
        *,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Optional[TraceEvaluationResultsResponse]:
        return await self._read_results(id, timeout=timeout)

    async def _read_results(
        self, id: str, *, timeout: float | httpx.Timeout | None, **request: Any
    ) -> Optional[TraceEvaluationResultsResponse]:
        try:
            resp = await self._get(f"{self._base_url()}/{id}/results", timeout=timeout, cast_to=dict, **request)
        except NotFoundError:
            return None
        data = _unwrap(resp)
        if not data or not isinstance(data, dict):
            return None

        try:
            return TraceEvaluationResultsResponse(**data)
        except Exception:
            return None

    async def wait_for_completion(
        self,
        id: str,
        *,
        interval_seconds: int = 3,
        timeout_seconds: Optional[int] = 300,
    ) -> Optional[TraceEvaluationResultsResponse]:
        """Poll every ``interval_seconds`` until the trace evaluation finishes, then return its results.

        See :meth:`TraceEvaluations.wait_for_completion`.
        """
        policy = _wait_policy(id, interval_seconds, timeout_seconds)
        finished = False
        while True:
            try:
                if not finished:
                    finished = _finished(await self._read(id, timeout=DEFAULT_TIMEOUT, max_retries=POLL_RETRIES))
                if finished:
                    return await self._read_results(id, timeout=DEFAULT_TIMEOUT, max_retries=POLL_RETRIES)
            except LayerLensError as err:
                delay = policy.after_error(err)
            else:
                delay = policy.after_pending()
            await self._sleep(delay)

    async def estimate_cost(
        self,
        *,
        trace_ids: List[str],
        judge_id: str,
        timeout: float | httpx.Timeout | None = DEFAULT_TIMEOUT,
    ) -> Optional[CostEstimateResponse]:
        resp = await self._post(
            f"{self._base_url()}/estimate",
            body={"trace_ids": trace_ids, "judge_id": judge_id},
            timeout=timeout,
            cast_to=dict,
        )
        data = _unwrap(resp)
        if isinstance(data, dict):
            try:
                return CostEstimateResponse(**data)
            except Exception:
                return None
        return None
