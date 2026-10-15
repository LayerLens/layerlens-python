"""Interleaved-run isolation for the CrewAI adapter (LAY-3576 / A6).

Two runs through ONE adapter instance whose lifecycle events interleave
(start A, start B, events A, events B, end A, end B) must produce two
uploaded traces with distinct trace_ids, each containing exactly its own
run's events — no cross-contamination, no lost events.

THE BUG (now fixed): the adapter used to store run state in instance
scalars (``self._collector`` / ``self._crew_span_id`` / ...), so a second
``kickoff()`` clobbered the first run's collector — cross-tenant trace
corruption.

THE FIX: crewai stamps every event with ``event_id`` + ``parent_event_id``
and chains them into a tree rooted at the ``CrewKickoffStartedEvent`` (verified
live for two interleaved concurrent kickoffs — the bus dispatches every handler
inside its own ``contextvars.copy_context()``, so the parent-chain is the only
stable per-run key). The adapter now keeps one ``RunState`` per root event_id
and resolves each event to its owning run by walking that chain
(``crewai.py`` — ``_runs`` / ``_resolve_run`` / ``_dispatch``).

DRIVING: these tests fire REAL crewai event objects through the REAL
``crewai_event_bus``, each run inside its OWN ``contextvars.Context`` (what two
concurrent ``crew.kickoff()`` calls have, on threads or asyncio tasks). Nothing
is hand-chained: the bus stamps ``parent_event_id`` / ``started_event_id`` from
that run's own scope stack, exactly as at runtime. That matters for the
completion event. Real crewai stamps ``CrewKickoffCompletedEvent`` with
``parent_event_id=None`` (it closes the top-level scope) and only
``started_event_id`` ties it to its kickoff. These fakes used to hand-set
``parent_event_id=root`` on it, which crewai never does, and so missed that the
adapter dropped every completion while a second run was open (LAY-4241).
"""

from __future__ import annotations

import sys
import json
import threading
import contextvars
from typing import Any, Dict, List
from collections import Counter

import pytest

from .conftest import record_for_schema_lock

if sys.version_info < (3, 10):
    pytest.skip("crewai requires Python >= 3.10", allow_module_level=True)

pytest.importorskip("crewai.events")

from crewai.events import (  # noqa: E402
    TaskStartedEvent,
    TaskCompletedEvent,
    CrewKickoffStartedEvent,
    CrewKickoffCompletedEvent,
    crewai_event_bus,
)

# crewai stamps the event_id / parent_event_id lineage this isolation guard keys
# on only from ~1.14 onward; the base lock can resolve an older crewai (e.g.
# 1.6.x) whose events carry no lineage, so per-run isolation cannot be expressed
# or tested there. The adapter degrades gracefully on such versions (it falls
# back to a generated span id — see crewai.py), and this invariant is exercised
# against the pinned crewai==1.15.27 in the adapter matrix (frameworks.toml).
if "event_id" not in CrewKickoffStartedEvent.model_fields:
    pytest.skip(
        "crewai lacks event_id/parent_event_id lineage (<1.14); per-run isolation "
        "requires it — exercised in the matrix on crewai==1.15.27",
        allow_module_level=True,
    )

from crewai.tasks.task_output import TaskOutput  # noqa: E402

from layerlens.instrument.adapters.frameworks.crewai import CrewAIAdapter  # noqa: E402

# -- Helpers --


def _collect_traces(mock_client: Any) -> List[Dict[str, Any]]:
    """Set up mock_client to accumulate SEPARATE trace payloads per upload."""
    traces: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def _capture(path: str) -> None:
        with open(path) as f:
            data = json.load(f)
        with lock:
            traces.append(data[0])
            record_for_schema_lock(data[0].get("events", []))

    mock_client.traces.upload.side_effect = _capture
    return traces


def _assert_isolated_traces(
    traces: List[Dict[str, Any]],
    markers: List[str],
    expected_counts: Dict[str, int],
) -> None:
    """The LAY-3576 invariant: one trace per run, keyed by content marker.

    Each marker must appear in exactly one uploaded trace; that trace must
    contain none of the other runs' markers, and exactly its own run's
    events (by event_type counts).
    """
    assert len(traces) == len(markers), (
        f"Expected {len(markers)} uploaded traces (one per run), got {len(traces)} — "
        "interleaved runs merged or lost a trace"
    )
    trace_ids = {t["trace_id"] for t in traces}
    assert len(trace_ids) == len(markers), f"Traces must have distinct trace_ids, got {trace_ids}"

    for marker in markers:
        own = [t for t in traces if marker in json.dumps(t["events"])]
        assert len(own) == 1, f"Run marker {marker!r} must appear in exactly 1 trace, found in {len(own)}"
        trace = own[0]
        blob = json.dumps(trace["events"])
        for other in markers:
            if other != marker:
                assert other not in blob, f"Trace for run {marker!r} is contaminated with run {other!r} events"
        counts = Counter(e["event_type"] for e in trace["events"])
        assert counts == Counter(expected_counts), (
            f"Trace for run {marker!r} lost or gained events: got {dict(counts)}, expected {expected_counts}"
        )
        assert all(e["trace_id"] == trace["trace_id"] for e in trace["events"]), (
            "Events within a trace must share its trace_id"
        )


def _emit(run_ctx: contextvars.Context, event: Any) -> None:
    """Emit through the REAL bus inside ``run_ctx`` and block until every handler ran.

    The bus stamps the event's lineage from ``run_ctx``'s own scope stack.
    """
    fut = run_ctx.run(crewai_event_bus.emit, None, event)
    if fut is not None:
        fut.result(timeout=10.0)


def _start_crew(run_ctx: contextvars.Context, marker: str) -> None:
    """Fire the run's crew kickoff."""
    _emit(run_ctx, CrewKickoffStartedEvent(crew_name=f"{marker}-crew", inputs={"topic": f"{marker}-topic"}))


def _crew_events(run_ctx: contextvars.Context, marker: str) -> None:
    """Fire the run's mid-lifecycle events: one task start + complete."""
    _emit(
        run_ctx,
        TaskStartedEvent(context=f"{marker} context", task_name=f"{marker}-task", agent_role=f"{marker}-researcher"),
    )
    output = TaskOutput(description=f"{marker}-task", raw=f"{marker}-task-result", agent=f"{marker}-researcher")
    _emit(run_ctx, TaskCompletedEvent(output=output, task_name=f"{marker}-task"))


def _end_crew(run_ctx: contextvars.Context, marker: str) -> None:
    """Fire the run's crew completion (agent.output + flush)."""
    output = TaskOutput(description="final", raw=f"{marker}-final-result", agent=f"{marker}-researcher")
    _emit(run_ctx, CrewKickoffCompletedEvent(crew_name=f"{marker}-crew", output=output))


#: Events one run produces: crew agent.input/agent.output + task agent.input/agent.output.
_EXPECTED_RUN_COUNTS = {
    "agent.input": 2,
    "agent.output": 2,
    "agent.identity": 1,  # canonical declared-identity marker, once per trace (from the crew_name)
}


# -- Fixtures --


@pytest.fixture
def adapter(mock_client):
    """Create a connected CrewAI adapter inside a scoped event-bus context."""
    a = CrewAIAdapter(mock_client)
    with crewai_event_bus.scoped_handlers():
        a.connect()
        yield a
    a.disconnect()


# -- Tests --


class TestInterleavedRunIsolation:
    def test_sequential_runs_are_isolated(self, adapter, mock_client):
        """GREEN baseline: back-to-back runs produce two clean traces."""
        traces = _collect_traces(mock_client)

        ra, rb = contextvars.Context(), contextvars.Context()
        _start_crew(ra, "alpha")
        _crew_events(ra, "alpha")
        _end_crew(ra, "alpha")

        _start_crew(rb, "bravo")
        _crew_events(rb, "bravo")
        _end_crew(rb, "bravo")

        _assert_isolated_traces(traces, ["alpha", "bravo"], _EXPECTED_RUN_COUNTS)

    def test_interleaved_runs_are_isolated(self, adapter, mock_client):
        """start A, start B, events A, events B, end A, end B → 2 clean traces.

        Single-thread interleave: both runs are open at once, so the adapter
        cannot fall back to "the one open run" — it must attribute each event
        to its own run by lineage, including each completion that only
        ``started_event_id`` links to its kickoff. Reverting the keyed-run fix
        (back to a single ``self._collector``) turns this RED, and so does
        resolving runs by ``parent_event_id`` alone.
        """
        traces = _collect_traces(mock_client)

        ra, rb = contextvars.Context(), contextvars.Context()
        _start_crew(ra, "alpha")  # start A
        _start_crew(rb, "bravo")  # start B (would clobber A under the old scalar)
        _crew_events(ra, "alpha")  # events A
        _crew_events(rb, "bravo")  # events B
        _end_crew(ra, "alpha")  # end A
        _end_crew(rb, "bravo")  # end B

        _assert_isolated_traces(traces, ["alpha", "bravo"], _EXPECTED_RUN_COUNTS)

    def test_concurrent_threaded_runs_are_isolated(self, adapter, mock_client):
        """Two real concurrent kickoffs on worker threads → 2 clean traces.

        The faithful production workload: two ``crew.kickoff()`` equivalents
        racing through the one shared adapter + the real event bus. A barrier
        forces their lifecycle phases to interleave on the bus.
        """
        traces = _collect_traces(mock_client)
        barrier = threading.Barrier(2)
        errors: List[BaseException] = []

        def run(marker: str) -> None:
            run_ctx = contextvars.Context()
            try:
                _start_crew(run_ctx, marker)
                barrier.wait(timeout=10)
                _crew_events(run_ctx, marker)
                barrier.wait(timeout=10)
                _end_crew(run_ctx, marker)
            except BaseException as exc:  # noqa: BLE001 — surface to the test thread
                errors.append(exc)

        ta = threading.Thread(target=run, args=("alpha",))
        tb = threading.Thread(target=run, args=("bravo",))
        ta.start()
        tb.start()
        ta.join(timeout=30)
        tb.join(timeout=30)

        assert not errors, f"worker thread raised: {errors}"
        _assert_isolated_traces(traces, ["alpha", "bravo"], _EXPECTED_RUN_COUNTS)
