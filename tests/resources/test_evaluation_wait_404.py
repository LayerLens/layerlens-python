"""d1: the evaluation wait's docstring said it returns None when the evaluation can't be read any more.

A 404 mid-wait (the evaluation is gone) raises ``NotFoundError`` at once, on every side; the
docstring now says so, and that None comes only from a reply that holds no evaluation.
"""

from __future__ import annotations

import json
import asyncio
from pathlib import Path

import httpx
import pytest

from layerlens import LayerLens, NotFoundError, AsyncLayerLens
from layerlens.models import Evaluation
from layerlens.resources.evaluations.evaluations import Evaluations

CORPUS = Path(__file__).resolve().parents[1] / "contract" / "corpus" / "evaluations_get_many.json"


def _evaluation() -> Evaluation:
    return Evaluation.model_validate(json.loads(CORPUS.read_text())["evaluations"][0])


def _gone(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"status": "error", "error": "evaluation not found"})


def test_a_404_mid_wait_raises_not_found() -> None:
    client = LayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = "o", "p"
    client._transport = httpx.MockTransport(_gone)
    with pytest.raises(NotFoundError):
        client.evaluations.wait_for_completion(_evaluation(), interval_seconds=1, timeout_seconds=5)


def test_the_async_wait_too() -> None:
    client = AsyncLayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = "o", "p"
    client._transport = httpx.MockTransport(_gone)
    with pytest.raises(NotFoundError):
        asyncio.run(client.evaluations.wait_for_completion(_evaluation(), interval_seconds=1, timeout_seconds=5))


def test_the_docstring_says_a_404_raises() -> None:
    doc = " ".join((Evaluations.wait_for_completion.__doc__ or "").split())
    assert ":class:`NotFoundError` when no evaluation has this id any more (a 404)" in doc
    assert "cannot be read any more" not in doc
