"""m12: a 402 (no credit left) was a plain ``APIStatusError``, under no name the errors guide gives.

When the organization has no credit left, credit-gated creates (benchmark and trace evaluations,
judge optimizations, synthetic jobs) answer ``402 {"status": "error", "error": "insufficient credit
amount"}``. The SDK raised it as a bare ``APIStatusError``, so a caller could tell it from any other
status only by its number. It is now ``layerlens.PaymentRequiredError``, a subclass of
``APIStatusError`` like ``RateLimitError``, on every client.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

import layerlens
from layerlens import LayerLens, PublicClient, AsyncLayerLens, AsyncPublicClient

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
SENTENCE = "insufficient credit amount"


def _no_credit(request: httpx.Request) -> httpx.Response:
    return httpx.Response(402, json={"status": "error", "error": SENTENCE})


def _scoped(client: Any) -> Any:
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(_no_credit)
    return client


def _check(err: Any) -> None:
    assert type(err) is layerlens.PaymentRequiredError
    assert isinstance(err, layerlens.APIStatusError) and not isinstance(err, layerlens.RateLimitError)
    assert err.status_code == 402
    assert err.body == SENTENCE


def test_the_client_raises_payment_required_on_a_402() -> None:
    client = _scoped(LayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0))
    with pytest.raises(layerlens.PaymentRequiredError) as caught:
        client.integrations.get_many()
    _check(caught.value)


def test_the_async_client_too() -> None:
    client = _scoped(AsyncLayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0))
    with pytest.raises(layerlens.PaymentRequiredError) as caught:
        asyncio.run(client.integrations.get_many())
    _check(caught.value)


@pytest.mark.parametrize("cls", [PublicClient, AsyncPublicClient])
def test_the_public_clients_too(cls: Any) -> None:
    client = cls(api_key="k", base_url="https://api.test/api/v1", max_retries=0)
    client._transport = httpx.MockTransport(_no_credit)
    call = client.models.get
    with pytest.raises(layerlens.PaymentRequiredError) as caught:
        result = call()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    _check(caught.value)


def test_a_402_is_not_retried() -> None:
    sent = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _no_credit(request)

    client = LayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=2)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(answer)
    with pytest.raises(layerlens.PaymentRequiredError):
        client.integrations.get_many()
    assert len(sent) == 1
