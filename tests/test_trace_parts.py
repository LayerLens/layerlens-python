"""A trace continued in another process: ``Trace`` keeps the parts the API names.

Since layerlens-app#2888 (D-2) a trace continued in another process with
``trace_context(from_context=...)`` is stored as one trace per process, its parts. The traces
list shows it once, through its first part, with ``segment_count``; the read of a part names
every part (``segments``), its place (``part``) and their number (``parts``). ``Trace`` dropped
all four, so the SDK could not reach a later part at all: the list hides it, and only
``segments`` names it. The fields are optional: an older API, and a trace that is not
continued, send none of them.

The clients are real; only their transport is an ``httpx.MockTransport``.
"""

from __future__ import annotations

import copy
import asyncio
from typing import Any, Dict, List

import httpx
import pytest

from layerlens import LayerLens, AsyncLayerLens
from layerlens.models import Trace, TraceSegment, TraceWithEvaluations

ORG = "691254ecd44a29f6926881a2"
PROJECT = "691254eed44a29f6926881a3"
TRACES = f"/api/v1/organizations/{ORG}/projects/{PROJECT}/traces"
ROOT_ID = "6a9200000000000000000001"
CONTINUATION_ID = "6a9200000000000000000002"

# GET /traces/{id} of the second part, as atlas-app's models.Trace and TraceSegment encode it.
CONTINUED = {
    "id": CONTINUATION_ID,
    "organization_id": ORG,
    "project_id": PROJECT,
    "user_id": "owner@example.com",
    "created_at": "2026-11-07T10:00:01Z",
    "filename": "layerlens_trace_b.json",
    "data": {"trace_id": "418a9fda15974477"},
    "tags": ["receiver"],
    "segments": [
        {
            "id": ROOT_ID,
            "segment_id": "a" * 32,
            "start_time": "2026-11-07T10:00:00Z",
            "event_count": 4,
            "attestation_status": "verified",
        },
        {
            "id": CONTINUATION_ID,
            "segment_id": "b" * 32,
            "continued_from_span_id": "0123456789abcdef",
            "start_time": "2026-11-07T10:00:01Z",
            "event_count": 2,
            "attestation_status": "verified",
        },
    ],
    "part": 2,
    "parts": 2,
}

# The same trace in the list: its first part, with how many parts it has.
LISTED = {
    "id": ROOT_ID,
    "organization_id": ORG,
    "project_id": PROJECT,
    "created_at": "2026-11-07T10:00:00Z",
    "filename": "layerlens_trace_a.json",
    "segment_count": 2,
    "evaluations_count": 0,
    "last_evaluations": [],
}


def _answer(body: Dict[str, Any]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == TRACES:
            data: Any = {"traces": [body], "count": 1, "total_count": 1}
        elif request.url.path == f"{TRACES}/{body['id']}":
            data = body
        else:
            return httpx.Response(404, text="404 page not found")
        return httpx.Response(200, json={"status": "success", "data": data})

    return httpx.MockTransport(handle)


def _client(body: Dict[str, Any]) -> LayerLens:
    client = LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = _answer(body)
    return client


def _assert_names_both_parts(trace: Any) -> None:
    assert isinstance(trace, Trace)
    assert (trace.part, trace.parts) == (2, 2)
    assert trace.segments is not None
    assert [s.id for s in trace.segments] == [ROOT_ID, CONTINUATION_ID]
    root, continuation = trace.segments
    assert isinstance(root, TraceSegment)
    assert root.continued_from_span_id is None and root.segment_id == "a" * 32
    assert continuation.continued_from_span_id == "0123456789abcdef"
    assert (continuation.event_count, continuation.attestation_status) == (2, "verified")
    assert continuation.start_time == "2026-11-07T10:00:01Z"


def test_the_read_of_a_part_names_every_part_of_its_trace() -> None:
    _assert_names_both_parts(_client(CONTINUED).traces.get(CONTINUATION_ID, strict=True))


def test_the_async_read_of_a_part_names_every_part_too() -> None:
    client = AsyncLayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = _answer(CONTINUED)

    async def read() -> Any:
        async with client:
            return await client.traces.get(CONTINUATION_ID, strict=True)

    _assert_names_both_parts(asyncio.run(read()))


def test_a_listed_trace_says_how_many_parts_it_has() -> None:
    page = _client(LISTED).traces.get_many(strict=True)
    assert page is not None
    (row,) = page.traces
    assert isinstance(row, TraceWithEvaluations)
    assert row.segment_count == 2


def test_the_continued_trace_dump_holds_the_parts() -> None:
    # `layerlens trace get --format json` prints the model's dump.
    trace = _client(CONTINUED).traces.get(CONTINUATION_ID, strict=True)
    assert trace is not None
    dumped = trace.model_dump()
    assert dumped["parts"] == 2 and dumped["segments"][1]["id"] == CONTINUATION_ID


def test_a_trace_that_is_not_continued_or_an_older_api_reply_has_no_parts() -> None:
    plain = {k: v for k, v in CONTINUED.items() if k not in ("segments", "part", "parts")}
    trace = _client(plain).traces.get(CONTINUATION_ID, strict=True)
    assert trace is not None
    assert (trace.segments, trace.part, trace.parts, trace.segment_count) == (None, None, None, None)


@pytest.mark.parametrize("key", ["segments", "part", "parts", "segment_count"])
def test_a_null_part_field_still_parses(key: str) -> None:
    body = copy.deepcopy(CONTINUED)
    body[key] = None
    trace = _client(body).traces.get(CONTINUATION_ID, strict=True)
    assert trace is not None and getattr(trace, key) is None


@pytest.mark.parametrize("key", ["continued_from_span_id", "start_time", "event_count", "attestation_status"])
def test_a_segment_without_an_optional_field_still_parses(key: str) -> None:
    body = copy.deepcopy(CONTINUED)
    segments: List[Dict[str, Any]] = body["segments"]
    segments[1].pop(key)
    trace = _client(body).traces.get(CONTINUATION_ID, strict=True)
    assert trace is not None and trace.segments is not None
    assert getattr(trace.segments[1], key) is None
