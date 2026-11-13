"""``client.public`` is built with the options of the client it comes from: retries too.

``client.public`` passed the key, URL and timeout on, but not ``max_retries``, so
``LayerLens(max_retries=0).public`` still retried a 503 twice, and ``max_retries=5`` stopped at
two retries (docs re-run 2026-11-10, L1R-037). The public client takes ``max_retries`` now.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import httpx
import pytest

from layerlens import LayerLens, AsyncLayerLens
from layerlens._exceptions import InternalServerError

BASE = "https://api.test/api/v1"


def _unavailable(seen: List[str]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(503, json={"message": "Service Unavailable"})

    return httpx.MockTransport(handle)


@pytest.fixture(autouse=True)
def _no_retry_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_pause(_seconds: float) -> None:
        return None

    monkeypatch.setattr("layerlens._base_client.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("asyncio.sleep", no_pause)


@pytest.mark.parametrize("retries", [0, 1, 4])
def test_the_public_client_retries_as_often_as_its_client_does(retries: int) -> None:
    seen: List[str] = []
    public = LayerLens(api_key="test-key", base_url=BASE, max_retries=retries).public
    public._transport = _unavailable(seen)  # type: ignore[assignment]
    with pytest.raises(InternalServerError):  # the 503, once the retries are spent
        public.models.get()
    assert len(seen) == retries + 1


@pytest.mark.parametrize("retries", [0, 4])
def test_the_async_public_client_retries_as_often_as_its_client_does(retries: int) -> None:
    seen: List[str] = []
    public: Any = AsyncLayerLens(api_key="test-key", base_url=BASE, max_retries=retries).public
    public._transport = _unavailable(seen)

    async def call() -> None:
        await public.models.get()

    with pytest.raises(InternalServerError):
        asyncio.run(call())
    assert len(seen) == retries + 1
