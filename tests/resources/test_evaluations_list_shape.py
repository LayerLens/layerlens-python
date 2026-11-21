"""m7: a 200 without an ``evaluations`` array read as "no evaluations".

``evaluations.get_many()`` read a body without the array as an empty page, so ``layerlens evaluate
list`` printed "No evaluations found." and ``ci report --latest`` reported an empty project, both
exiting 0 (1.11.0's ``ci report --limit`` exited 1 there). The API always sends the key, as
``null`` for an empty page, so a body without it is now an ``APIResponseValidationError``.

The CLI half (``evaluate list`` and ``ci report`` exit 1) is in
``tests/cli/test_evaluations_list_shape_cli.py``: this directory runs in the adapter matrix's
``core`` row, which installs no extras (no ``click``).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import httpx
import pytest

from layerlens import LayerLens, AsyncLayerLens
from layerlens._exceptions import APIResponseValidationError

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"


def _answering(body: Any) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json=body))


def _client(body: Any) -> LayerLens:
    client = LayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = _answering(body)
    return client


def _async_client(body: Any) -> AsyncLayerLens:
    client = AsyncLayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = _answering(body)
    return client


NOT_A_PAGE = [{"total_count": 3}, {"status": "success", "data": []}, {"evaluations": {"id": "x"}}, {"evaluations": "x"}]


@pytest.mark.parametrize("body", NOT_A_PAGE)
def test_a_body_without_an_evaluations_array_raises(body: Dict[str, Any]) -> None:
    with pytest.raises(APIResponseValidationError, match="evaluations"):
        _client(body).evaluations.get_many()


@pytest.mark.parametrize("body", NOT_A_PAGE)
def test_the_async_client_raises_too(body: Dict[str, Any]) -> None:
    with pytest.raises(APIResponseValidationError, match="evaluations"):
        asyncio.run(_async_client(body).evaluations.get_many())


@pytest.mark.parametrize("rows", [None, []])
def test_a_null_or_empty_array_is_still_an_empty_page(rows: Any) -> None:
    page = _client({"evaluations": rows, "total_count": 0}).evaluations.get_many()
    assert page is not None and page.evaluations == []
