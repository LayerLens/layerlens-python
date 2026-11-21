"""A list page holds at most what the API serves: 100 rows.

The API clamps every list's ``page_size`` to 100 (its ``ClampPageSize``). The SDK allowed 500 and
counted pages from the size it asked for: ``evaluations.get_many(page_size=500)`` got 100 of 131
rows and reported one page, so paging by ``total_pages`` skipped 31 rows. The fake API here clamps
as the real one does.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Callable
from pathlib import Path

import httpx
import pytest

from layerlens import LayerLens

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
CORPUS = Path(__file__).resolve().parents[1] / "contract" / "corpus"
TOTAL = 131


def _evaluation_rows() -> List[Dict[str, Any]]:
    row = json.loads((CORPUS / "evaluations_get_many.json").read_text())["evaluations"][0]
    return [{**copy.deepcopy(row), "id": f"{i:024x}"} for i in range(TOTAL)]


def _result_rows() -> List[Dict[str, Any]]:
    row = json.loads((CORPUS / "results_get_flat_metrics.json").read_text())["results"][0]
    return [{**copy.deepcopy(row), "prompt": f"prompt {i}"} for i in range(TOTAL)]


class _ClampingAPI:
    """Serves TOTAL rows a page at a time, clamping page_size to 100 as the API does."""

    def __init__(self) -> None:
        self.sizes_asked: List[str] = []
        self.evaluations = _evaluation_rows()
        self.results = _result_rows()

    def handle(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        self.sizes_asked.append(params.get("page_size", ""))
        size = min(int(params.get("page_size", "20")), 100)
        page = int(params.get("page", "1"))
        window = slice((page - 1) * size, page * size)
        path = request.url.path
        if path.endswith("/results"):
            body: Any = {
                "evaluation_id": "e1",
                "results": self.results[window],
                "metrics": {"total_count": TOTAL},
            }
        elif path.endswith("/evaluations"):
            body = {"evaluations": self.evaluations[window], "total_count": TOTAL}
        elif path.endswith("/models"):
            body = {"models": [], "count": 0, "total_count": 0}
        elif path.endswith("/datasets"):
            body = {"datasets": [], "count": 0, "total_count": 0}
        else:
            body = {"status": "success", "data": []}
        return httpx.Response(200, json=body)


@pytest.fixture
def api() -> _ClampingAPI:
    return _ClampingAPI()


def _client(api: _ClampingAPI) -> LayerLens:
    client = LayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(api.handle)
    client.public._transport = httpx.MockTransport(api.handle)
    return client


PAGED: "Dict[str, Callable[[LayerLens, int], Any]]" = {
    "evaluations": lambda c, page: c.evaluations.get_many(page=page, page_size=500),
    "public.evaluations": lambda c, page: c.public.evaluations.get_many(page=page, page_size=500),
    "results": lambda c, page: c.results.get_by_id(evaluation_id="e1", page=page, page_size=500),
}


@pytest.mark.parametrize("read", list(PAGED.values()), ids=list(PAGED))
def test_paging_by_total_pages_reads_every_row(api: _ClampingAPI, read: Callable[[LayerLens, int], Any]) -> None:
    client = _client(api)

    first = read(client, 1)
    assert (first.pagination.page_size, first.pagination.total_pages) == (100, 2)
    read_rows: List[str] = []
    for page in range(1, first.pagination.total_pages + 1):
        response = read(client, page)
        if hasattr(response, "evaluations"):
            read_rows.extend(e.id for e in response.evaluations)
        else:
            read_rows.extend(r.prompt for r in response.results)

    # Every row once, in order, across the page boundary (rows 100 and 101 included).
    served = [e["id"] for e in api.evaluations] if read is not PAGED["results"] else [r["prompt"] for r in api.results]
    assert len(read_rows) == TOTAL
    assert read_rows == served
    assert set(api.sizes_asked) == {"100"}


LISTS: "Dict[str, Callable[[LayerLens], Any]]" = {
    "traces": lambda c: c.traces.get_many(page_size=500),
    "judges": lambda c: c.judges.get_many(page_size=500),
    "scorers": lambda c: c.scorers.get_many(page_size=500),
    "integrations": lambda c: c.integrations.get_many(page_size=500),
    "trace_evaluations": lambda c: c.trace_evaluations.get_many(page_size=500),
    "judge_optimizations": lambda c: c.judge_optimizations.get_many(page_size=500),
    "public.models": lambda c: c.public.models.get(page_size=500),
    "public.benchmarks": lambda c: c.public.benchmarks.get(page_size=500),
}


@pytest.mark.parametrize("read", list(LISTS.values()), ids=list(LISTS))
def test_no_list_asks_for_more_than_the_api_serves(api: _ClampingAPI, read: Callable[[LayerLens], Any]) -> None:
    read(_client(api))

    assert api.sizes_asked == ["100"]
