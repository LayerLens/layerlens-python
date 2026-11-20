"""What a get or a delete does with an id nothing has, as the docstrings now say (R2 F3, R3 F4).

They raise NotFoundError: the docs said they return None or False. Three are different and
say so: ``scorers.delete`` returns False (it raises nothing), ``trace_evaluations.get_results``
returns None, and a ``get_by_key`` returns None when no model or benchmark has the key. The 404s
are the API's own replies for an unknown id (local stack, API main, 2026-11-17).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx
import pytest

from layerlens import LayerLens, NotFoundError, AsyncLayerLens

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
MISSING = "000000000000000000000000"


def _not_found(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models") or request.url.path.endswith("/benchmarks"):
        # A key lookup lists the project's models or benchmarks: none has the key.
        return httpx.Response(200, json={"status": "success", "data": {"models": [], "datasets": []}})
    return httpx.Response(404, json={"status": "error", "error": f"record with ID '{MISSING}' not found"})


def _client() -> LayerLens:
    client = LayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(_not_found)
    client.public._transport = httpx.MockTransport(_not_found)
    return client


RAISES: "dict[str, Callable[[Any], Any]]" = {
    "models.get_by_id": lambda c: c.models.get_by_id(MISSING),
    "benchmarks.get_by_id": lambda c: c.benchmarks.get_by_id(MISSING),
    "evaluations.get_by_id": lambda c: c.evaluations.get_by_id(MISSING),
    "public.evaluations.get_by_id": lambda c: c.public.evaluations.get_by_id(MISSING),
    "results.get_by_id": lambda c: c.results.get_by_id(evaluation_id=MISSING),
    "integrations.get": lambda c: c.integrations.get(MISSING),
    "judge_optimizations.get": lambda c: c.judge_optimizations.get(MISSING),
    "judges.get": lambda c: c.judges.get(MISSING),
    "judges.delete": lambda c: c.judges.delete(MISSING),
    "scorers.get": lambda c: c.scorers.get(MISSING),
    "trace_evaluations.get": lambda c: c.trace_evaluations.get(MISSING),
    "traces.get": lambda c: c.traces.get(MISSING),
    "traces.delete": lambda c: c.traces.delete(MISSING),
}


@pytest.mark.parametrize("call", list(RAISES.values()), ids=list(RAISES))
def test_an_unknown_id_raises_not_found(call: Callable[[Any], Any]) -> None:
    with pytest.raises(NotFoundError):
        call(_client())


def test_scorers_delete_returns_false_and_raises_nothing() -> None:
    assert _client().scorers.delete(MISSING) is False


def test_get_results_of_an_unknown_trace_evaluation_is_none() -> None:
    assert _client().trace_evaluations.get_results(MISSING) is None


@pytest.mark.parametrize("lookup", ["models", "benchmarks"])
def test_get_by_key_of_an_unknown_key_is_none(lookup: str) -> None:
    assert getattr(_client(), lookup).get_by_key("no-such-key") is None


def test_the_async_client_raises_the_same() -> None:
    client = AsyncLayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(_not_found)

    async def main() -> None:
        with pytest.raises(NotFoundError):
            await client.judges.get(MISSING)
        with pytest.raises(NotFoundError):
            await client.traces.delete(MISSING)
        assert await client.scorers.delete(MISSING) is False

    asyncio.run(main())
