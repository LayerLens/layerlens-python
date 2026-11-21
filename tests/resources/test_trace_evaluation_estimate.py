"""``trace_evaluations.estimate_cost()`` returned None for every estimate the API gave.

``CostEstimateResponse`` required a ``model`` field the API never sends, and the validation error
was swallowed, so the docs' ``estimate.estimated_cost`` raised ``AttributeError`` on None. The API
(``trace_evaluations_estimate.go``) answers ``trace_count`` and ``cost_available`` always, and
``estimated_cost``, ``input_tokens``, ``output_tokens`` and ``unpriced_judges`` when it could price
the run. The replies below are what the local API answered, as the E2E recorded them.
"""

from __future__ import annotations

import json
import asyncio
from typing import Any, Dict, List, Optional

import httpx
import pytest

from layerlens import LayerLens, AsyncLayerLens
from layerlens.models import CostEstimateResponse

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
ESTIMATE_PATH = f"/api/v1/organizations/{ORG}/projects/{PROJECT}/trace-evaluations/estimate"

#: What the API answered for one trace and one judge it could price.
PRICED = {
    "cost_available": True,
    "estimated_cost": 0.0012126,
    "input_tokens": 4926,
    "output_tokens": 1800,
    "trace_count": 1,
    "unpriced_judges": 0,
}
#: What it answers when pricing failed (a judge on a model the catalogue no longer carries).
UNPRICED = {"cost_available": False, "trace_count": 3}


def _api(reply: Dict[str, Any], seen: List[Dict[str, Any]]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == ESTIMATE_PATH:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "success", "data": reply})
        return httpx.Response(404, text="404 page not found")

    return httpx.MockTransport(handle)


def _client(reply: Dict[str, Any], seen: List[Dict[str, Any]]) -> LayerLens:
    client = LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = _api(reply, seen)
    return client


def test_a_priced_estimate_is_read() -> None:
    seen: List[Dict[str, Any]] = []
    estimate = _client(PRICED, seen).trace_evaluations.estimate_cost(trace_ids=["t1"], judge_id="j1")
    assert isinstance(estimate, CostEstimateResponse)
    assert (estimate.estimated_cost, estimate.input_tokens, estimate.output_tokens) == (0.0012126, 4926, 1800)
    assert (estimate.trace_count, estimate.cost_available, estimate.unpriced_judges) == (1, True, 0)
    assert estimate.model is None
    assert seen == [{"trace_ids": ["t1"], "judge_id": "j1"}]


def test_an_estimate_the_api_could_not_price_says_so() -> None:
    estimate = _client(UNPRICED, []).trace_evaluations.estimate_cost(trace_ids=["a", "b", "c"], judge_id="j1")
    assert estimate is not None
    assert (estimate.cost_available, estimate.trace_count) == (False, 3)
    assert (estimate.estimated_cost, estimate.input_tokens, estimate.output_tokens) == (None, None, None)


@pytest.mark.parametrize("reply", [PRICED, UNPRICED])
def test_the_async_client_reads_the_same(reply: Dict[str, Any]) -> None:
    client = AsyncLayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = _api(reply, [])

    async def main() -> Optional[CostEstimateResponse]:
        return await client.trace_evaluations.estimate_cost(trace_ids=["t1"], judge_id="j1")

    estimate = asyncio.run(main())
    assert estimate is not None
    assert estimate.cost_available is reply["cost_available"]
    assert estimate.estimated_cost == reply.get("estimated_cost")


@pytest.mark.parametrize(
    "reply, available",
    [({k: v for k, v in PRICED.items() if k != "cost_available"}, True), ({"trace_count": 2}, False)],
    ids=["priced", "unpriced"],
)
def test_a_reply_without_cost_available_says_it_by_its_price(reply: Dict[str, Any], available: bool) -> None:
    estimate = _client(reply, []).trace_evaluations.estimate_cost(trace_ids=["t1"], judge_id="j1")
    assert estimate is not None and estimate.cost_available is available


def test_an_unpriced_estimate_is_falsy_so_the_1_11_guard_never_formats_a_missing_price() -> None:
    """R2 F1: 1.11.0's docs guard the format with `if estimate:`; 1.11.0 always returned None."""
    unpriced = _client(UNPRICED, []).trace_evaluations.estimate_cost(trace_ids=["a"], judge_id="j1")
    priced = _client(PRICED, []).trace_evaluations.estimate_cost(trace_ids=["a"], judge_id="j1")

    printed = [f"Estimated cost: ${e.estimated_cost:.4f}" for e in (unpriced, priced) if e]

    assert unpriced is not None and not unpriced
    assert priced is not None and priced
    assert printed == ["Estimated cost: $0.0012"]
