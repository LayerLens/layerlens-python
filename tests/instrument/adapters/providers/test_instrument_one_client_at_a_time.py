"""``instrument_<provider>()`` instruments one client of that provider at a time.

This pins behaviour the functions now document; it is not a change. The module-level functions keep
one adapter per provider in the adapter registry (``register`` replaces the adapter of that name),
and ``uninstrument_<provider>()`` takes no client: it restores the one instrumented last. So
instrumenting a second client restores the first (docs E2E 2026-11-10, L3 ST-00). To trace several
clients at once, connect an adapter to each.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

from layerlens.instrument import trace
from layerlens.instrument.adapters.providers.openai import (
    OpenAIProvider,
    instrument_openai,
    uninstrument_openai,
)

from .conftest import make_openai_response
from ...conftest import find_events


def _openai_client() -> Mock:
    client = Mock()
    client.chat.completions.create = Mock(return_value=make_openai_response(model="gpt-4o-mini"))
    return client


def test_instrumenting_a_second_client_restores_the_first() -> None:
    first, second = _openai_client(), _openai_client()
    first_create, second_create = first.chat.completions.create, second.chat.completions.create
    try:
        instrument_openai(first)
        instrument_openai(second)
        assert first.chat.completions.create is first_create
        assert second.chat.completions.create is not second_create
    finally:
        uninstrument_openai()
    assert second.chat.completions.create is second_create


def test_an_adapter_per_client_traces_every_client(mock_client: Any, capture_trace: Any) -> None:
    first, second = _openai_client(), _openai_client()
    adapters = [OpenAIProvider(), OpenAIProvider()]
    adapters[0].connect(first)
    adapters[1].connect(second)

    @trace(mock_client)
    def agent() -> None:
        for client in (first, second):
            client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])

    try:
        agent()
    finally:
        for adapter in adapters:
            adapter.disconnect()
    assert len(find_events(capture_trace["events"], "model.invoke")) == 2
