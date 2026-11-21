"""A wait for an evaluation ends when the evaluation is cancelled.

``Evaluation.is_finished`` counted success, failure and timeout, but not ``cancelled``, so
``wait_for_completion`` on a cancelled evaluation polled on forever with the default (no) timeout,
and so did ``evaluate run --wait`` and ``bulk eval --wait`` (docs E2E 2026-11-10, L2 B6). The API's
own ``EvaluationStatus.IsFinished`` (``apps/shared/models/evaluation.go``) is success, failure,
cancelled and timeout; ``pending`` and ``in-progress`` are the others it has. ``paused`` is not an
API status, but the SDK's enum names it, and an evaluation paused is not finished.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Union

import httpx
import pytest

from layerlens import LayerLens, AsyncLayerLens, _polling
from layerlens.models import Evaluation, EvaluationStatus
from layerlens._resource import SyncAPIResource, AsyncAPIResource

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
EVALUATION_ID = "68a1f0c2d4e5b6a7c8d9e001"
EVALUATION_PATH = f"/api/v1/evaluations/{EVALUATION_ID}"


def _evaluation(status: str) -> Dict[str, Any]:
    return {
        "id": EVALUATION_ID,
        "status": status,
        "submitted_at": 1752710400,
        "finished_at": 0,
        "model_id": "68a1f0c2d4e5b6a7c8d9e010",
        "dataset_id": "68a1f0c2d4e5b6a7c8d9e020",
        "average_duration": 0,
        "accuracy": 0.0,
    }


def test_an_evaluation_is_finished_in_the_statuses_the_api_calls_finished() -> None:
    finished = {status.value for status in EvaluationStatus if Evaluation(**_evaluation(status.value)).is_finished}
    assert finished == {"success", "failure", "timeout", "cancelled"}
    assert set(EvaluationStatus) - {EvaluationStatus(s) for s in finished} == {
        EvaluationStatus.PENDING,
        EvaluationStatus.IN_PROGRESS,
        EvaluationStatus.PAUSED,
    }


class _Clock:
    """Time that moves only when the wait sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()

    async def async_sleep(_self: Any, seconds: float) -> None:
        fake.sleep(seconds)

    monkeypatch.setattr(_polling, "time", fake)
    monkeypatch.setattr(SyncAPIResource, "_sleep", lambda _self, seconds: fake.sleep(seconds))
    monkeypatch.setattr(AsyncAPIResource, "_sleep", async_sleep)
    return fake


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_the_wait_returns_the_cancelled_evaluation_at_once(kind: str, clock: _Clock) -> None:
    polls: List[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        polls.append(request.url.path)
        if request.url.path == EVALUATION_PATH:
            return httpx.Response(200, json=_evaluation("cancelled"))
        return httpx.Response(404, text="404 page not found")

    cls = LayerLens if kind == "sync" else AsyncLayerLens
    client: Union[LayerLens, AsyncLayerLens] = cls(api_key="test-key", base_url="https://api.test/api/v1")
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(handle)  # type: ignore[assignment]

    result: Any = client.evaluations.wait_for_completion(
        Evaluation(**_evaluation("in-progress")), interval_seconds=30, timeout_seconds=600
    )
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    assert result is not None and result.status is EvaluationStatus.CANCELLED
    assert polls == [EVALUATION_PATH]
    assert clock.sleeps == []
