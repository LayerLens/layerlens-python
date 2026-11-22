"""``client.public.benchmarks.get()`` reads a public benchmark's descriptions.

The API's ``/datasets`` rows carry ``full_description`` and ``short_description`` (``shared/models/
dataset.go``), never ``description``, so ``PublicBenchmarkDetail.description`` was always None
(docs re-run 2026-11-10, PA-07), where the project-side ``PublicBenchmark`` reads ``full_description``.
The row below is the local API's first ``/datasets`` row, as it answered, shortened.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import httpx

from layerlens import PublicClient, AsyncPublicClient
from layerlens.models import PublicBenchmarkDetail

ROW: Dict[str, Any] = {
    "id": "6a0b6964ed57a8fd01d147c0",
    "key": "twodmovement",
    "name": "2D Movement",
    "full_description": "Dataset to Test the model's ability to keep track of position in 2D space.",
    "short_description": "Navigation and spatial reasoning in 2D space",
    "categories": ["Spatial Reasoning"],
    "language": "",
    "prompt_count": 100,
    "deprecated": False,
    "created_at": 1747000000,
}
#: The public ``/datasets`` reply has no envelope.
PAGE = {"datasets": [ROW], "categories": [], "languages": [], "count": 1, "total_count": 1}


def _api() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/datasets":
            return httpx.Response(200, json=PAGE)
        return httpx.Response(404, text="404 page not found")

    return httpx.MockTransport(handle)


def _assert_described(benchmark: PublicBenchmarkDetail) -> None:
    assert benchmark.description == ROW["full_description"]
    assert benchmark.short_description == ROW["short_description"]


def test_a_public_benchmark_has_its_descriptions() -> None:
    client = PublicClient(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client._transport = _api()  # type: ignore[assignment]
    page = client.benchmarks.get()
    assert page is not None
    _assert_described(page.datasets[0])


def test_the_async_client_reads_them_too() -> None:
    client: Any = AsyncPublicClient(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client._transport = _api()

    async def main() -> Any:
        return await client.benchmarks.get()

    page = asyncio.run(main())
    assert page is not None
    _assert_described(page.datasets[0])


def test_a_row_that_says_description_is_read_as_well() -> None:
    detail = PublicBenchmarkDetail.model_validate({"id": "b", "key": "k", "name": "n", "description": "plain"})
    assert detail.description == "plain"
    assert PublicBenchmarkDetail.model_validate(ROW).model_dump()["description"] == ROW["full_description"]
