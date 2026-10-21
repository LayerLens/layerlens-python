from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple
from unittest.mock import Mock

# Re-export from root conftest so framework tests can do `from .conftest import ...`
from ...conftest import find_event, find_events, record_for_schema_lock  # noqa: F401


def capture_framework_trace(mock_client: Mock) -> Dict[str, Any]:
    """Capture the uploaded trace payload from a framework adapter.

    Accumulates events across multiple flushes (some adapters use
    multiple collectors). Uploaded events are recorded for the schema lock,
    which is enforced after the test by the ``_enforce_schema_lock`` autouse
    fixture (validating inside the mocked upload is swallowed — see the note in
    the root ``conftest.py``; LAY-3613).
    """
    uploaded: Dict[str, Any] = {"events": []}

    def _capture(path: str) -> None:
        with open(path) as f:
            data = json.load(f)
        payload = data[0]
        uploaded["trace_id"] = payload.get("trace_id")
        uploaded["events"].extend(payload.get("events", []))
        uploaded["capture_config"] = payload.get("capture_config", {})
        uploaded["attestation"] = payload.get("attestation", {})
        record_for_schema_lock(payload.get("events", []))

    mock_client.traces.upload.side_effect = _capture
    return uploaded


def model_calls(events: List[Dict[str, Any]]) -> List[Tuple[Any, ...]]:
    """``(model, tokens_prompt, tokens_completion, tokens_total, finish_reason)`` of
    each model.invoke, in emission order, so a test can pin every provider call a
    trace reports, and how many there are, in one comparison."""
    return [
        (
            e["payload"].get("model"),
            e["payload"].get("tokens_prompt"),
            e["payload"].get("tokens_completion"),
            e["payload"].get("tokens_total"),
            e["payload"].get("finish_reason"),
        )
        for e in find_events(events, "model.invoke")
    ]


def assert_priced_once_per_call(events: List[Dict[str, Any]], totals: List[int], model: str) -> None:
    """One cost.record per provider call: their ``tokens_total`` are ``totals`` in
    emission order, each is on ``model``, priced, and hung off a model.invoke
    reporting the same total (the server sums every cost.record, so a second one
    for the same call doubles the cost)."""
    costs = find_events(events, "cost.record")
    assert [c["payload"]["tokens_total"] for c in costs] == totals
    invokes = {e["span_id"]: e for e in find_events(events, "model.invoke")}
    for cost in costs:
        assert cost["payload"]["model"] == model
        assert cost["payload"]["cost_usd"] > 0
        invoke = invokes[cost["parent_span_id"]]
        assert invoke["payload"]["tokens_total"] == cost["payload"]["tokens_total"]


def assert_one_closed_tree(events: List[Dict[str, Any]]) -> str:
    """Every event hangs off a span the trace captured, and the trace has one root.

    The server builds the span tree (and the agent graph) from ``parent_span_id``;
    a parent no event carries is a dangling one, which the collector can only
    paper over with a synthesized ``trace.root`` marker, and only when there is
    exactly one such parent. Returns the root span id."""
    assert not find_events(events, "trace.root"), "a dangling parent made the collector synthesize a root"
    captured = {e["span_id"] for e in events}
    roots = {e["span_id"] for e in events if e["parent_span_id"] in (None, e["span_id"])}
    assert len(roots) == 1, f"expected one root span, got {roots}"
    for e in events:
        parent = e["parent_span_id"]
        if parent not in (None, e["span_id"]):
            assert parent in captured, f"{e['event_type']} hangs off span {parent}, which no event carries"
    return roots.pop()
