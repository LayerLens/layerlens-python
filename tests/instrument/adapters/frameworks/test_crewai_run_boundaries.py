"""One crewai execution is one trace (LAY-4241).

Every test here drives REAL crewai objects (``Crew.kickoff()``, a ``Flow``
subclass, ``Agent.kickoff()``) through the real ``crewai_event_bus`` with the
real ``CrewAIAdapter``; the only mock is the network boundary (an
``httpx.MockTransport`` serving the recorded OpenAI chat completion). Uploads are
collected PER TRACE, so a split execution shows up as two uploads instead of
being merged back together by the helper (``capture_framework_trace`` concatenates
every upload's events, which is how the split below went unnoticed by the
recorded-replay test).

What broke
----------
From crewai 1.14.7 each agent execution runs inside an internal Flow
(``flow_name="AgentExecutor"``). The adapter opened a NEW run for every
``FlowStartedEvent``, and that flow's ``FlowFinishedEvent`` walked its lineage
back to the crew root and ended the CREW's trace early. One ``crew.kickoff()``
reached the customer as two traces: the crew's inputs in one, its model call and
cost in the other.

Two more splits had the same cause: the adapter decided "new run" by event type,
never by lineage. A crew (or a standalone agent) run from inside a customer's own
Flow became separate traces, and a kickoff's ``CrewKickoffCompletedEvent`` (which
crewai stamps with ``parent_event_id=None``; only ``started_event_id`` links it to
its start) could not be attributed while a second kickoff was in flight, so
concurrent kickoffs lost their completion and flushed only at disconnect.
"""

from __future__ import annotations

import os
import sys
import json
import threading
import contextvars
from types import SimpleNamespace
from typing import Any, Dict, List, Type, Callable, Optional
from collections import Counter

import pytest

from tests._optional_deps import importorskip

if sys.version_info < (3, 10):
    pytest.skip("crewai requires Python >= 3.10", allow_module_level=True)
crewai = importorskip("crewai")

from packaging.version import Version  # noqa: E402

# The keyless native-provider seam and the typed event bus with event lineage
# both arrive on the 1.14 line (see test_crewai_floor.py / test_crewai_recorded.py).
if Version(crewai.__version__) < Version("1.14"):
    pytest.skip(
        f"crewai run-boundary tests require >= 1.14; got {crewai.__version__}",
        allow_module_level=True,
    )

import httpx  # noqa: E402
import crewai.events as crewai_events  # noqa: E402
from crewai import LLM, Crew, Task, Agent, Process  # noqa: E402
from crewai.events import (  # noqa: E402
    FlowStartedEvent,
    FlowFinishedEvent,
    ToolUsageStartedEvent,
    CrewKickoffStartedEvent,
    CrewKickoffCompletedEvent,
    AgentExecutionStartedEvent,
    crewai_event_bus,
)
from crewai.flow.flow import Flow, start, listen  # noqa: E402

import openai  # noqa: E402
from layerlens.attestation._verify import verify_chain  # noqa: E402
from layerlens.attestation._envelope import HashScope, AttestationEnvelope  # noqa: E402
from layerlens.instrument._capture_config import CaptureConfig  # noqa: E402
from layerlens.instrument.adapters.frameworks import crewai as crewai_adapter  # noqa: E402
from layerlens.instrument.adapters.frameworks.crewai import CrewAIAdapter  # noqa: E402

from .conftest import record_for_schema_lock  # noqa: E402
from ..._recorded import load_recorded, mock_transport  # noqa: E402

#: crewai runs every agent execution inside an internal ``AgentExecutor`` Flow
#: from this release on.
_NESTED_EXECUTOR_FLOW = Version(crewai.__version__) >= Version("1.14.7")

#: ``usage.total_tokens`` of the recorded OpenAI completion (tests/fixtures/recorded/openai/default.json).
_RECORDED_TOTAL_TOKENS = 13


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_traces(mock_client: Any) -> List[Dict[str, Any]]:
    """Capture each uploaded trace payload separately (one list entry per upload).

    Answers like a backend that accepted the trace: a result without ``trace_ids``
    counts as a rejected upload, and enough of those open the upload circuit breaker.
    """
    traces: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def _capture(path: str) -> SimpleNamespace:
        with open(path) as f:
            payload = json.load(f)[0]
        with lock:
            traces.append(payload)
            record_for_schema_lock(payload.get("events", []))
        return SimpleNamespace(trace_ids=[payload["trace_id"]])

    mock_client.traces.upload.side_effect = _capture
    return traces


def _llm(before_response: Optional[Callable[[], None]] = None) -> LLM:
    """A real ``crewai.LLM`` (native OpenAI provider) over the recorded completion.

    ``before_response`` runs inside the transport before the recorded response is
    returned; the concurrency test uses it to hold both kickoffs open at once.
    """
    transport, _ = mock_transport(load_recorded("openai", "default"))
    if before_response is not None:
        recorded = transport.handler

        def handler(request: httpx.Request) -> httpx.Response:
            before_response()
            return recorded(request)

        transport = httpx.MockTransport(handler)
    # No key at construction skips the provider's eager async-client build (which
    # rejects a sync httpx.Client); the public api_key field feeds the lazy sync build.
    prior_key = os.environ.pop("OPENAI_API_KEY", None)
    try:
        llm = LLM(model="openai/gpt-4o-mini", client_params={"http_client": httpx.Client(transport=transport)})
    finally:
        if prior_key is not None:
            os.environ["OPENAI_API_KEY"] = prior_key
    llm.api_key = "test-key"
    return llm


def _agent(role: str, llm: LLM) -> Agent:
    return Agent(
        role=role, goal="Reply to the user", backstory="A terse assistant.", llm=llm, verbose=False, max_iter=1
    )


def _crew(tag: str, llm: Optional[LLM] = None) -> Crew:
    agent = _agent(tag, llm or _llm())
    task = Task(description=f"Reply with exactly: pong ({tag})", expected_output="pong", agent=agent)
    return Crew(name=f"{tag}-crew", agents=[agent], tasks=[task], process=Process.sequential, verbose=False)


def _of_type(events: List[Dict[str, Any]], event_type: str) -> List[Dict[str, Any]]:
    return [e for e in events if e["event_type"] == event_type]


def _one(events: List[Dict[str, Any]], event_type: str, **payload: Any) -> Dict[str, Any]:
    """The single event of ``event_type`` whose payload carries every ``payload`` item."""
    hits = [e for e in _of_type(events, event_type) if all(e["payload"].get(k) == v for k, v in payload.items())]
    assert len(hits) == 1, f"expected exactly one {event_type} with {payload}, got {len(hits)}: {_shape(events)}"
    return hits[0]


def _shape(events: List[Dict[str, Any]]) -> List[str]:
    return [f"{e['event_type']}:{e.get('span_name') or ''}" for e in events]


def _assert_one_connected_tree(trace: Dict[str, Any], root_span: str) -> None:
    """Every event shares the trace's id, ``root_span`` is the only root, and no
    event points at a parent span that is not in this trace."""
    events = trace["events"]
    assert {e["trace_id"] for e in events} == {trace["trace_id"]}
    roots = {e["span_id"] for e in events if e.get("parent_span_id") is None}
    assert roots == {root_span}, f"expected the single root span {root_span}, got roots {roots}: {_shape(events)}"
    spans = {e["span_id"] for e in events}
    dangling = [e for e in events if e.get("parent_span_id") is not None and e["parent_span_id"] not in spans]
    assert not dangling, f"events parented to a span outside this trace: {_shape(dangling)}"


def _assert_attestation_covers_every_event(trace: Dict[str, Any]) -> None:
    raw = ((trace.get("attestation") or {}).get("chain") or {}).get("events") or []
    envelopes = [
        AttestationEnvelope(hash=e["hash"], scope=HashScope(e["scope"]), previous_hash=e.get("previous_hash"))
        for e in raw
    ]
    assert len(envelopes) == len(trace["events"]), (
        f"attestation chain has {len(envelopes)} envelopes for {len(trace['events'])} events"
    )
    result = verify_chain(envelopes)
    assert result.valid, f"attestation chain invalid: {result.error}"


# ---------------------------------------------------------------------------
# A single kickoff
# ---------------------------------------------------------------------------


class TestKickoffIsOneTrace:
    def test_real_kickoff_uploads_one_complete_trace(self, mock_client):
        traces = _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            result = _crew("responder").kickoff()
            crewai_event_bus.flush(timeout=10.0)
            # The run must end on the kickoff's own completion event, not be
            # swept up by disconnect().
            uploaded_before_disconnect = len(traces)
            adapter.disconnect()

        assert str(result) == "pong"
        assert uploaded_before_disconnect == 1
        assert len(traces) == 1, f"one kickoff uploaded {len(traces)} traces: {[_shape(t['events']) for t in traces]}"

        trace = traces[0]
        events = trace["events"]
        crew_in = _one(events, "agent.input", crew_name="responder-crew")
        crew_span = crew_in["span_id"]
        _assert_one_connected_tree(trace, crew_span)

        # Crew input, the model call, its cost and the crew output are all in it.
        crew_out = _one(events, "agent.output", crew_name="responder-crew")
        assert crew_out["span_id"] == crew_span
        assert crew_out["payload"]["tokens_total"] == _RECORDED_TOTAL_TOKENS
        assert _one(events, "model.invoke")["payload"]["model"] == "gpt-4o-mini"
        assert _one(events, "cost.record")["payload"]["tokens_total"] == _RECORDED_TOTAL_TOKENS
        assert _one(events, "agent.output", agent_role="responder")["payload"]["output"] == "pong"

        _assert_attestation_covers_every_event(trace)

    @pytest.mark.skipif(not _NESTED_EXECUTOR_FLOW, reason="crewai < 1.14.7 has no internal AgentExecutor flow")
    def test_agent_executor_flow_is_a_span_under_its_agent(self, mock_client):
        traces = _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            _crew("responder").kickoff()
            crewai_event_bus.flush(timeout=10.0)
            adapter.disconnect()

        assert len(traces) == 1
        events = traces[0]["events"]
        agent_span = _one(events, "agent.input", agent_role="responder")["span_id"]
        flow_in = _one(events, "agent.input", flow_name="AgentExecutor")
        flow_out = _one(events, "agent.output", flow_name="AgentExecutor")

        assert flow_in["parent_span_id"] == agent_span
        assert flow_out["span_id"] == flow_in["span_id"]
        assert flow_out["parent_span_id"] == agent_span
        # The model call happens inside the executor flow, so it hangs off that span.
        assert _one(events, "model.invoke")["parent_span_id"] == flow_in["span_id"]

    @pytest.mark.skipif(not _NESTED_EXECUTOR_FLOW, reason="crewai < 1.14.7 has no internal AgentExecutor flow")
    def test_executor_flow_nests_even_when_its_agent_start_is_handled_late(self, mock_client):
        """crewai runs each event's handlers as a separate thread-pool task, so the
        adapter can handle the executor flow's start before the agent start it sits
        under (a slow co-listener on AgentExecutionStartedEvent is enough). The flow
        must still land in the crew's trace, and the run must still close when the
        kickoff completes, rather than leaving an orphan run that only
        ``disconnect()`` uploads."""
        traces = _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        flow_handled = threading.Event()
        dispatch = adapter._dispatch

        def agent_start_handled_after_the_flow(target: Any, nested_target: Any, source: Any, event: Any) -> None:
            # Everything the adapter does for the agent start (lineage included)
            # waits until it has handled the executor flow's start.
            if isinstance(event, AgentExecutionStartedEvent):
                flow_handled.wait(timeout=10)
            try:
                dispatch(target, nested_target, source, event)
            finally:
                if isinstance(event, FlowStartedEvent):
                    flow_handled.set()

        adapter._dispatch = agent_start_handled_after_the_flow  # type: ignore[method-assign]
        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            _crew("responder").kickoff()
            crewai_event_bus.flush(timeout=10.0)
            uploaded_before_disconnect = len(traces)
            adapter.disconnect()

        assert flow_handled.is_set(), "the executor flow never started"
        assert uploaded_before_disconnect == 1, f"got {[_shape(t['events']) for t in traces]}"
        assert len(traces) == 1, f"the late-handled flow split the kickoff: {[_shape(t['events']) for t in traces]}"
        trace = traces[0]
        events = trace["events"]
        _assert_one_connected_tree(trace, _one(events, "agent.input", crew_name="responder-crew")["span_id"])
        assert _one(events, "agent.input", flow_name="AgentExecutor")["parent_span_id"] is not None
        assert _one(events, "model.invoke")["payload"]["model"] == "gpt-4o-mini"
        assert _one(events, "cost.record")["payload"]["tokens_total"] == _RECORDED_TOTAL_TOKENS

    @pytest.mark.skipif(not _NESTED_EXECUTOR_FLOW, reason="crewai < 1.14.7 has no internal AgentExecutor flow")
    def test_a_late_handled_flow_never_leaves_its_run_open(self, mock_client, monkeypatch):
        """Without crewai's scope stack (a private name a future release could
        drop), lineage comes only from the adapter's own handlers, so a late
        agent-start handler leaves the executor flow nothing to nest under and it
        opens a run of its own. The trace splits, but that run must still close
        on the flow's own end event rather than linger until ``disconnect()``
        (or forever, in a process that never disconnects)."""
        monkeypatch.setattr(crewai_adapter, "_crewai_scope_stack", None)
        traces = _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        flow_handled = threading.Event()
        dispatch = adapter._dispatch

        def agent_start_handled_after_the_flow(target: Any, nested_target: Any, source: Any, event: Any) -> None:
            if isinstance(event, AgentExecutionStartedEvent):
                flow_handled.wait(timeout=10)
            try:
                dispatch(target, nested_target, source, event)
            finally:
                if isinstance(event, FlowStartedEvent):
                    flow_handled.set()

        adapter._dispatch = agent_start_handled_after_the_flow  # type: ignore[method-assign]
        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            _crew("responder").kickoff()
            crewai_event_bus.flush(timeout=10.0)
            uploaded_before_disconnect, open_runs = len(traces), len(adapter._runs)
            adapter.disconnect()

        assert open_runs == 0
        assert uploaded_before_disconnect == len(traces), "a run was only uploaded by disconnect()"
        flow_traces = [t for t in traces if any(e["payload"].get("flow_name") == "AgentExecutor" for e in t["events"])]
        assert len(flow_traces) == 1
        assert _one(flow_traces[0]["events"], "agent.output", flow_name="AgentExecutor")

    def test_adapter_retains_no_lineage_once_its_runs_end(self, mock_client):
        """The adapter lives as long as the process; what it records to attribute a
        run's events must go when the run ends, not accumulate per kickoff."""
        _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            for _ in range(3):
                # A fresh context per kickoff, as separate threads or tasks have: the
                # main thread's crewai scope stack may still hold scopes that earlier
                # bare-event tests left open.
                contextvars.Context().run(_crew("responder").kickoff)
                crewai_event_bus.flush(timeout=10.0)
            open_runs, parents, roots = len(adapter._runs), len(adapter._event_parents), len(adapter._event_root)
            adapter.disconnect()

        assert (open_runs, parents, roots) == (0, 0, 0)


# ---------------------------------------------------------------------------
# A customer's own Flow
# ---------------------------------------------------------------------------


def _run_flow(mock_client: Any, body: Callable[[], str]) -> List[Dict[str, Any]]:
    """Kick off a two-step customer Flow whose first step runs ``body``."""

    class CustomerFlow(Flow):
        @start()
        def begin(self) -> str:
            return body()

        @listen(begin)
        def finish(self, value: str) -> str:
            return f"done:{value}"

    traces = _collect_traces(mock_client)
    adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
    with crewai_event_bus.scoped_handlers():
        adapter.connect()
        # A fresh context, as a request handler's thread or task has (see
        # test_adapter_retains_no_lineage_once_its_runs_end).
        contextvars.Context().run(CustomerFlow().kickoff)
        crewai_event_bus.flush(timeout=10.0)
        uploaded_before_disconnect = len(traces)
        retained = (len(adapter._runs), len(adapter._event_parents), len(adapter._event_root))
        adapter.disconnect()

    assert uploaded_before_disconnect == 1, (
        f"the flow must upload exactly one trace when it finishes, got {uploaded_before_disconnect}: "
        f"{[_shape(t['events']) for t in traces]}"
    )
    assert len(traces) == 1
    # Nothing about the finished flow (method executions included) is kept.
    assert retained == (0, 0, 0)
    return traces


class TestCustomerFlowIsTheRun:
    def test_top_level_flow_stays_a_root_run(self, mock_client):
        def plain() -> str:
            return "plain"

        trace = _run_flow(mock_client, plain)[0]
        events = trace["events"]
        flow_in = _one(events, "agent.input", flow_name="CustomerFlow")
        flow_out = _one(events, "agent.output", flow_name="CustomerFlow")
        assert flow_in["parent_span_id"] is None
        assert flow_out["span_id"] == flow_in["span_id"]
        assert flow_out["payload"]["output"] == "done:plain"
        _assert_one_connected_tree(trace, flow_in["span_id"])

    def test_crew_kicked_off_inside_a_flow_is_part_of_its_trace(self, mock_client):
        def crew_step() -> str:
            return str(_crew("responder").kickoff())

        trace = _run_flow(mock_client, crew_step)[0]
        events = trace["events"]
        flow_span = _one(events, "agent.input", flow_name="CustomerFlow")["span_id"]
        _assert_one_connected_tree(trace, flow_span)

        crew_in = _one(events, "agent.input", crew_name="responder-crew")
        crew_out = _one(events, "agent.output", crew_name="responder-crew")
        assert crew_in["parent_span_id"] == flow_span
        assert crew_out["span_id"] == crew_in["span_id"]
        assert crew_out["parent_span_id"] == flow_span
        # The crew's task hangs off the crew, not the flow.
        task_in = [e for e in _of_type(events, "agent.input") if "task_name" in e["payload"]]
        assert [e["parent_span_id"] for e in task_in] == [crew_in["span_id"]]
        assert _one(events, "cost.record")["payload"]["tokens_total"] == _RECORDED_TOTAL_TOKENS
        assert _one(events, "agent.output", flow_name="CustomerFlow")["payload"]["output"] == "done:pong"
        _assert_attestation_covers_every_event(trace)

    def test_agent_kicked_off_inside_a_flow_is_part_of_its_trace(self, mock_client):
        def agent_step() -> str:
            return str(_agent("lite", _llm()).kickoff("Reply with exactly: pong"))

        trace = _run_flow(mock_client, agent_step)[0]
        events = trace["events"]
        flow_span = _one(events, "agent.input", flow_name="CustomerFlow")["span_id"]
        _assert_one_connected_tree(trace, flow_span)
        assert _one(events, "model.invoke")["payload"]["model"] == "gpt-4o-mini"
        assert _one(events, "agent.output", flow_name="CustomerFlow")["payload"]["output"] == "done:pong"

    def test_concurrent_crews_inside_a_flow_close_their_own_spans(self, mock_client):
        """``kickoff_for_each_async`` runs the same crew once per input, concurrently,
        inside the flow's run: same crew name, agent role and task label in both
        copies. Each copy's agent / task / crew output must close the span its own
        start opened (a span with an input and no output, or an output on a span
        nothing opened, is a broken tree)."""
        both_in_flight = threading.Barrier(2)

        def hold_until_both_in_flight() -> None:
            both_in_flight.wait(timeout=20)

        agent = _agent("worker", _llm(before_response=hold_until_both_in_flight))
        task = Task(description="Reply with exactly: pong ({x})", expected_output="pong", agent=agent)
        crew = Crew(name="each-crew", agents=[agent], tasks=[task], process=Process.sequential, verbose=False)

        class CustomerFlow(Flow):
            @start()
            async def begin(self) -> str:
                results = await crew.kickoff_for_each_async(inputs=[{"x": "a"}, {"x": "b"}])
                return ",".join(str(r) for r in results)

        traces = _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            CustomerFlow().kickoff()
            crewai_event_bus.flush(timeout=10.0)
            uploaded_before_disconnect = len(traces)
            adapter.disconnect()

        assert uploaded_before_disconnect == 1
        assert len(traces) == 1
        events = traces[0]["events"]
        inputs = Counter(e["span_id"] for e in _of_type(events, "agent.input"))
        outputs = Counter(e["span_id"] for e in _of_type(events, "agent.output"))
        assert inputs == outputs, f"spans opened {dict(inputs)} vs closed {dict(outputs)}: {_shape(events)}"
        assert len(_of_type(events, "model.invoke")) == 2
        crews = _of_type(events, "agent.input")
        assert len([e for e in crews if e["payload"].get("crew_name") == "each-crew"]) == 2
        assert len([e for e in crews if e["payload"].get("agent_role") == "worker"]) == 2


# ---------------------------------------------------------------------------
# Failed flows
# ---------------------------------------------------------------------------


def _failing_llm() -> LLM:
    """A real ``crewai.LLM`` whose provider answers every call with an HTTP 400."""

    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "boom", "type": "invalid_request_error"}})

    prior_key = os.environ.pop("OPENAI_API_KEY", None)
    try:
        llm = LLM(
            model="openai/gpt-4o-mini",
            client_params={"http_client": httpx.Client(transport=httpx.MockTransport(reject))},
        )
    finally:
        if prior_key is not None:
            os.environ["OPENAI_API_KEY"] = prior_key
    llm.api_key = "test-key"
    return llm


@pytest.mark.skipif(
    not hasattr(crewai_events, "FlowFailedEvent"), reason="crewai < 1.15 emits no FlowFailedEvent for a failed flow"
)
class TestFailedFlow:
    """A failed flow never emits FlowFinishedEvent; its trace must still upload when
    it fails (with the error), not wait for ``disconnect()``."""

    def _kickoff(self, mock_client: Any, step: Callable[[], str], raises: Type[BaseException]) -> List[Dict[str, Any]]:
        class FailingFlow(Flow):
            @start()
            def begin(self) -> str:
                return step()

        traces = _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            with pytest.raises(raises):
                FailingFlow().kickoff()
            crewai_event_bus.flush(timeout=10.0)
            uploaded_before_disconnect = len(traces)
            adapter.disconnect()

        assert uploaded_before_disconnect == 1, f"got {[_shape(t['events']) for t in traces]}"
        assert len(traces) == 1
        return traces[0]["events"]

    def test_failed_flow_uploads_its_error_when_it_fails(self, mock_client):
        def raises() -> str:
            raise ValueError("flow step failed")

        events = self._kickoff(mock_client, raises, ValueError)
        flow_in = _one(events, "agent.input", flow_name="FailingFlow")
        error = _one(events, "agent.error", flow_name="FailingFlow")
        assert error["span_id"] == flow_in["span_id"]
        assert error["parent_span_id"] is None
        assert error["payload"]["error_type"] == "flow_error"
        assert "flow step failed" in error["payload"]["error"]

    def test_crew_failing_inside_a_flow_uploads_with_the_flow(self, mock_client):
        def failing_crew() -> str:
            agent = _agent("worker", _failing_llm())
            task = Task(description="Reply with exactly: pong", expected_output="pong", agent=agent)
            crew = Crew(name="failing-crew", agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
            return str(crew.kickoff())

        events = self._kickoff(mock_client, failing_crew, openai.BadRequestError)
        flow_span = _one(events, "agent.input", flow_name="FailingFlow")["span_id"]
        crew_in = _one(events, "agent.input", crew_name="failing-crew")
        assert crew_in["parent_span_id"] == flow_span
        crew_error = _one(events, "agent.error", crew_name="failing-crew")
        assert crew_error["span_id"] == crew_in["span_id"]
        assert _one(events, "agent.error", flow_name="FailingFlow")["span_id"] == flow_span
        # The agent's executor flows failed too; each one's span is closed by its error.
        executor_inputs = Counter(
            e["span_id"] for e in _of_type(events, "agent.input") if e["payload"].get("flow_name") == "AgentExecutor"
        )
        executor_errors = Counter(
            e["span_id"] for e in _of_type(events, "agent.error") if e["payload"].get("flow_name") == "AgentExecutor"
        )
        assert executor_inputs and executor_inputs == executor_errors


# ---------------------------------------------------------------------------
# Unbalanced scope stacks
# ---------------------------------------------------------------------------


def _emit_in(run_ctx: contextvars.Context, event: Any) -> None:
    """Emit on the real bus inside ``run_ctx`` (the bus stamps lineage from that
    context's scope stack) and wait for every handler."""
    fut = run_ctx.run(crewai_event_bus.emit, None, event)
    if fut is not None:
        fut.result(timeout=10.0)


class TestUnbalancedScopes:
    """crewai pairs an end event with whatever scope is on top of its stack and
    only warns ("Event pairing mismatch") when that is the wrong one, so a scope
    left open (an exception that skipped its completion event) makes the next end
    event carry the wrong ``started_event_id``. These drive real events through
    the real bus with deliberately unbalanced scopes."""

    def test_flow_end_paired_with_the_wrong_scope_still_closes_its_own_span(self, mock_client):
        traces = _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        run_ctx = contextvars.Context()
        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            _emit_in(run_ctx, CrewKickoffStartedEvent(crew_name="c", inputs={}))
            _emit_in(run_ctx, AgentExecutionStartedEvent.model_construct(agent_role="worker"))
            _emit_in(run_ctx, FlowStartedEvent(flow_name="AgentExecutor", inputs={}))
            # Left open: the flow's end event pops THIS scope instead of the flow's.
            _emit_in(run_ctx, ToolUsageStartedEvent(tool_name="search", tool_args={}, agent_key="w"))
            _emit_in(run_ctx, FlowFinishedEvent(flow_name="AgentExecutor", result="r", state={}))
            uploaded_after_flow_end = len(traces)
            _emit_in(run_ctx, CrewKickoffCompletedEvent(crew_name="c", output=None))
            uploaded_after_crew_end = len(traces)
            adapter.disconnect()

        assert uploaded_after_flow_end == 0, "the nested flow's end event ended the crew's run"
        assert uploaded_after_crew_end == 1
        events = traces[0]["events"]
        agent_span = _one(events, "agent.input", agent_role="worker")["span_id"]
        flow_in = _one(events, "agent.input", flow_name="AgentExecutor")
        flow_out = _one(events, "agent.output", flow_name="AgentExecutor")
        assert flow_in["parent_span_id"] == agent_span
        assert flow_out["span_id"] == flow_in["span_id"]
        assert _one(events, "agent.output", crew_name="c")["parent_span_id"] is None

    def test_flow_end_paired_with_the_crew_scope_does_not_end_the_crew_run(self, mock_client):
        """A flow end event this run never opened a flow for (here crewai pairs it
        with the crew's own scope) must not end the crew's run."""
        traces = _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        run_ctx = contextvars.Context()
        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            _emit_in(run_ctx, CrewKickoffStartedEvent(crew_name="c", inputs={}))
            _emit_in(run_ctx, FlowFinishedEvent(flow_name="Stray", result="r", state={}))
            uploaded_after_flow_end = len(traces)
            _emit_in(run_ctx, CrewKickoffCompletedEvent(crew_name="c", output=None))
            uploaded_after_crew_end = len(traces)
            adapter.disconnect()

        assert uploaded_after_flow_end == 0, "a stray flow end event ended the crew's run"
        assert uploaded_after_crew_end == 1
        events = traces[0]["events"]
        assert not [e for e in events if e["payload"].get("flow_name") == "Stray"]
        assert _one(events, "agent.output", crew_name="c")["parent_span_id"] is None


class _ChangedScopeStack:
    """Stands in for crewai's private scope-stack ContextVar after a release that
    changed what it holds."""

    def __init__(self, value: Any) -> None:
        self.value = value

    def get(self) -> Any:
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class TestScopeStackShape:
    """The scope stack is a private crewai name; a release could keep the name and
    change its contents. The adapter must then fall back to the lineage its own
    handlers record, not drop every event (each handler reads the stack first)."""

    @pytest.mark.parametrize(
        "stack",
        [
            pytest.param((("a", "crew_kickoff_started", 1), ("b", "task_started", 2)), id="3-tuples"),
            pytest.param(("2b4f3c6e-crew-scope-id", "8d1e9a07-task-scope-id"), id="bare-ids"),
            pytest.param((object(), object()), id="objects"),
            pytest.param(LookupError("no default"), id="no-default"),
        ],
    )
    def test_a_changed_scope_stack_falls_back_to_handler_lineage(self, mock_client, monkeypatch, stack):
        monkeypatch.setattr(crewai_adapter, "_crewai_scope_stack", _ChangedScopeStack(stack))
        traces = _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            _crew("responder").kickoff()
            crewai_event_bus.flush(timeout=10.0)
            uploaded_before_disconnect = len(traces)
            adapter.disconnect()

        assert uploaded_before_disconnect == 1, f"got {[_shape(t['events']) for t in traces]}"
        trace = traces[0]
        events = trace["events"]
        crew_in = _one(events, "agent.input", crew_name="responder-crew")
        _assert_one_connected_tree(trace, crew_in["span_id"])
        assert _one(events, "model.invoke")["payload"]["model"] == "gpt-4o-mini"
        assert _one(events, "agent.output", crew_name="responder-crew")["span_id"] == crew_in["span_id"]


class TestLineageEviction:
    def test_long_open_run_keeps_its_lineage_while_other_runs_come_and_go(self, mock_client, monkeypatch):
        """The adapter bounds what it remembers of event lineage. A run that stays
        open while other runs pass through the same adapter must still place its
        next nested flow in its own trace, however many events went by meanwhile.
        (The cap is lowered here so a few runs exceed it.)"""
        monkeypatch.setattr(crewai_adapter, "_MAX_TRACKED_EVENTS", 30)
        traces = _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        long_run = contextvars.Context()
        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            _emit_in(long_run, CrewKickoffStartedEvent(crew_name="long", inputs={}))
            _emit_in(long_run, AgentExecutionStartedEvent.model_construct(agent_role="worker"))
            # Other runs stay open alongside it, each adding lineage entries.
            for i in range(20):
                other = contextvars.Context()
                _emit_in(other, CrewKickoffStartedEvent(crew_name=f"other-{i}", inputs={}))
                _emit_in(other, AgentExecutionStartedEvent.model_construct(agent_role=f"other-{i}"))
            _emit_in(long_run, FlowStartedEvent(flow_name="AgentExecutor", inputs={}))
            _emit_in(long_run, FlowFinishedEvent(flow_name="AgentExecutor", result="r", state={}))
            sizes = (len(adapter._event_parents), len(adapter._event_root))
            adapter.disconnect()

        # 21 open runs hold far more lineage than the cap; both maps stay within it.
        assert max(sizes) <= 30, sizes
        long_traces = [t for t in traces if any(e["payload"].get("crew_name") == "long" for e in t["events"])]
        assert len(long_traces) == 1
        events = long_traces[0]["events"]
        agent_span = _one(events, "agent.input", agent_role="worker")["span_id"]
        assert _one(events, "agent.input", flow_name="AgentExecutor")["parent_span_id"] == agent_span
        assert _one(events, "agent.output", flow_name="AgentExecutor")["parent_span_id"] == agent_span
        assert not [t for t in traces if t["events"][0]["payload"].get("flow_name") == "AgentExecutor"], (
            "the long run's nested flow became a trace of its own"
        )


# ---------------------------------------------------------------------------
# Concurrent kickoffs through one adapter
# ---------------------------------------------------------------------------


class TestHandlersOutOfOrder:
    @pytest.mark.skipif(not _NESTED_EXECUTOR_FLOW, reason="the AgentExecutor flow arrives in crewai 1.14.7")
    def test_the_executor_flow_hangs_under_its_agent_when_the_agent_start_is_handled_late(self, mock_client):
        """crewai runs handlers on a pool of 10 threads, so the AgentExecutor flow's
        FlowStartedEvent can be handled before its agent's AgentExecutionStartedEvent has
        recorded the agent's span. The flow then hung under the task (CI saw it once, in
        TestConcurrentKickoffs). Holding the agent-start handler back makes that order
        certain: the flow's handler waits for the agent's span."""
        traces = _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        handle_agent_start = adapter._on_agent_execution_started

        def handled_late(source: Any, event: Any) -> None:
            threading.Event().wait(0.3)
            handle_agent_start(source, event)

        adapter._on_agent_execution_started = handled_late  # type: ignore[method-assign]
        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            _crew("alpha", _llm()).kickoff()
            crewai_event_bus.flush(timeout=10.0)
            adapter.disconnect()

        (trace,) = traces
        events = trace["events"]
        agent_span = _one(events, "agent.input", agent_role="alpha")["span_id"]
        assert _one(events, "agent.input", flow_name="AgentExecutor")["parent_span_id"] == agent_span
        _assert_one_connected_tree(trace, _one(events, "agent.input", crew_name="alpha-crew")["span_id"])


class TestConcurrentKickoffs:
    def test_overlapping_kickoffs_each_upload_one_complete_trace(self, mock_client):
        """Two real kickoffs whose lifetimes overlap: both LLM calls wait for each
        other inside the transport, so both runs are open when the first one
        completes. Each must still end on its own completion event, with every
        event (nested executor flows included) in its own trace."""
        both_in_flight = threading.Barrier(2)

        def hold_until_both_in_flight() -> None:
            both_in_flight.wait(timeout=20)

        traces = _collect_traces(mock_client)
        adapter = CrewAIAdapter(mock_client, capture_config=CaptureConfig.full())
        errors: List[BaseException] = []

        def kickoff(tag: str) -> None:
            try:
                _crew(tag, _llm(before_response=hold_until_both_in_flight)).kickoff()
            except BaseException as exc:  # noqa: BLE001 - surfaced to the test thread
                errors.append(exc)

        with crewai_event_bus.scoped_handlers():
            adapter.connect()
            threads = [threading.Thread(target=kickoff, args=(tag,)) for tag in ("alpha", "bravo")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            crewai_event_bus.flush(timeout=10.0)
            uploaded_before_disconnect = len(traces)
            adapter.disconnect()

        assert not errors, f"kickoff raised: {errors}"
        assert uploaded_before_disconnect == 2, (
            f"each kickoff must upload when it completes; got {uploaded_before_disconnect} before disconnect"
        )
        assert len(traces) == 2
        assert len({t["trace_id"] for t in traces}) == 2

        for tag, other in (("alpha", "bravo"), ("bravo", "alpha")):
            own = [t for t in traces if any(e["payload"].get("crew_name") == f"{tag}-crew" for e in t["events"])]
            assert len(own) == 1, f"{tag}'s crew events landed in {len(own)} traces"
            trace = own[0]
            events = trace["events"]
            assert other not in json.dumps(events), f"{tag}'s trace carries {other}'s events"
            crew_in = _one(events, "agent.input", crew_name=f"{tag}-crew")
            _assert_one_connected_tree(trace, crew_in["span_id"])
            assert _one(events, "agent.output", crew_name=f"{tag}-crew")["span_id"] == crew_in["span_id"]
            assert len(_of_type(events, "model.invoke")) == 1
            assert _one(events, "cost.record")["payload"]["tokens_total"] == _RECORDED_TOTAL_TOKENS
            if _NESTED_EXECUTOR_FLOW:
                agent_span = _one(events, "agent.input", agent_role=tag)["span_id"]
                assert _one(events, "agent.input", flow_name="AgentExecutor")["parent_span_id"] == agent_span
            _assert_attestation_covers_every_event(trace)
