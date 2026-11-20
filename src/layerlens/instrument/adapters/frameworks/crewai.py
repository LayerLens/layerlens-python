from __future__ import annotations

import time
import logging
import threading
from typing import Any, Dict, List, Tuple, Iterator, Optional

from ._utils import safe_serialize
from ._handoff import scrub_context
from ..._context import RunState, _current_run, _current_collector
from ..._collector import TraceCollector
from ._base_framework import FrameworkAdapter
from ..._capture_config import CaptureConfig
from ....attestation._hash import compute_hash

log = logging.getLogger(__name__)

# CrewAI's built-in delegation tools (case-insensitive substring match).
# https://docs.crewai.com/concepts/agents#agent-delegation
#
# Patterns are stored in the human-readable "space" form. At runtime crewai
# does NOT emit that form: the LLM-facing tool name is passed through
# ``crewai.utilities.string_utils.sanitize_tool_name`` (lowercase + underscores)
# before it lands on ``ToolUsageStartedEvent.tool_name`` (crewai/tools/tool_usage.py),
# so a real hierarchical crew emits ``delegate_work_to_coworker`` /
# ``ask_question_to_coworker``. ``_is_delegation_tool`` normalizes underscores
# back to spaces before matching so BOTH forms are detected.
_DELEGATION_TOOL_PATTERNS: Tuple[str, ...] = (
    "delegate work to coworker",
    "ask question to coworker",
)

# Cap on the child->parent event map so a long-running adapter never grows it
# unbounded if a run never emits its completion event. Old entries are evicted
# FIFO; in-flight runs keep their own root via the RunState map regardless.
_MAX_TRACKED_EVENTS = 10_000


def _is_delegation_tool(tool_name: Optional[str]) -> bool:
    if not tool_name:
        return False
    # Normalize the sanitized runtime form ("delegate_work_to_coworker") back to
    # the human-readable pattern form ("delegate work to coworker") so both the
    # raw tool label and the sanitized name crewai actually emits match.
    low = tool_name.lower().replace("_", " ")
    return any(pat in low for pat in _DELEGATION_TOOL_PATTERNS)


try:
    from crewai.events import (
        BaseEventListener as _BaseEventListener,
    )  # pyright: ignore[reportMissingImports]
except (ImportError, TypeError):
    _BaseEventListener = None

# crewai's stack of open event scopes (outermost first) for the current context.
# The bus runs each handler in a copy of the emitting context, so inside a handler
# this is the emitted event's full ancestry: it does not depend on the ancestors'
# own handlers having run yet (crewai runs each event's handlers as a separate
# thread-pool task), nor on the adapter subscribing to every scope event. It is a
# private crewai name (same shape on 1.14 and 1.15); without it the adapter falls
# back to the lineage its own handlers record.
try:
    from crewai.events.event_context import (
        _event_id_stack as _crewai_scope_stack,
    )  # pyright: ignore[reportMissingImports]
except (ImportError, TypeError):
    _crewai_scope_stack = None


def _scope_stack() -> List[Tuple[str, Optional[str]]]:
    """crewai's open scopes as ``(event_id, event_type)``, outermost first, or [] when the
    private stack is absent or holds something other than the ``(event_id,
    event_type)`` entries 1.14 and 1.15 use. A release can keep the name and
    change its contents; lineage then comes from the adapter's own handlers
    alone, rather than every handler failing on it."""
    if _crewai_scope_stack is None:
        return []
    try:
        stack = _crewai_scope_stack.get()
    except LookupError:
        return []
    if not isinstance(stack, (tuple, list)):
        return []
    scopes: List[Tuple[str, Optional[str]]] = []
    for entry in stack:
        if not (isinstance(entry, (tuple, list)) and entry and isinstance(entry[0], str)):
            log.debug("layerlens: unrecognised crewai scope stack entry %r; using handler lineage", entry)
            return []
        kind = entry[1] if len(entry) > 1 and isinstance(entry[1], str) else None
        scopes.append((entry[0], kind))
    return scopes


def _scope_stack_ids() -> List[str]:
    """The event ids of crewai's open scopes, outermost first (see :func:`_scope_stack`)."""
    return [event_id for event_id, _ in _scope_stack()]


# The scopes whose start the adapter records a span for: a crew, task, agent, flow or tool.
_SPAN_SCOPES = frozenset(
    {"crew_kickoff_started", "task_started", "agent_execution_started", "flow_started", "tool_usage_started"}
)

# How long a handler waits for the span of an ancestor scope whose handler is still running.
# crewai runs handlers on a pool of 10 threads, so a child event's can run before its
# parent's has recorded the parent's span: the AgentExecutor flow then hung under the task.
_ANCESTOR_SPAN_WAIT_SECONDS = 1.0


class CrewAIAdapter(FrameworkAdapter):
    """CrewAI adapter using the typed event bus API (crewai >= 1.0).

    Concurrency model (LAY-3576 / A6 fix)
    -------------------------------------
    CrewAI's event bus dispatches every handler inside its OWN
    ``contextvars.copy_context()`` (``CrewAIEventsBus.emit``), so handlers
    cannot share ContextVar state and the old single ``self._collector``
    scalar let a second concurrent ``kickoff()`` clobber the first run's
    collector — cross-tenant trace corruption.

    The stable per-run key is the **root** ``CrewKickoffStartedEvent.event_id``:
    crewai stamps every event with ``event_id`` + ``parent_event_id`` and chains
    them into a tree rooted at the kickoff event (verified live for two
    interleaved concurrent kickoffs). This adapter therefore keeps one
    ``RunState`` per root event_id in ``self._runs`` and, for every incoming
    event, walks the ``parent_event_id`` chain back to its owning root (an end
    event whose ``started_event_id`` opened a run belongs to that run) to find
    the right run — then binds ``_current_run`` / ``_current_collector`` for the
    duration of that one handler so ``_fire`` and the per-run state resolve
    correctly. This mirrors ``openai_agents.py`` (``_trace_runs`` keyed by
    ``trace_id`` + per-callback ``_current_collector.set``).

    Lineage also decides run boundaries (LAY-4241): a crew / flow start whose
    lineage resolves to an open run is a nested span of that run (crewai >=
    1.14.7's internal ``AgentExecutor`` flow, or a crew run from a customer's
    Flow), and only the event that opened a run ends its trace.

    Usage::

        adapter = CrewAIAdapter(client)
        adapter.connect()
        crew.kickoff()
        adapter.disconnect()
    """

    name = "crewai"
    # CrewAI 0.30+ is Pydantic v2-only (LAY-3447 catalog manifest AC).
    requires_pydantic: str = "2"

    def __init__(self, client: Any, capture_config: Optional[CaptureConfig] = None) -> None:
        super().__init__(client, capture_config)
        self._registered_handlers: list = []
        # root event_id -> RunState (one per concurrent kickoff/flow).
        self._runs: Dict[str, RunState] = {}
        # event_id -> parent_event_id, so any event can be traced to its root.
        self._event_parents: Dict[str, Optional[str]] = {}
        # Notified whenever a handler records the span it opened for an event.
        self._spans_recorded = threading.Condition()
        # event_id -> owning root event_id (memoised chain walk).
        self._event_root: Dict[str, str] = {}
        # Fallback collector slot for callers that drive ``_fire`` outside a run
        # (the cost-pricing invariant test instantiates via ``__new__`` and sets
        # ``adapter._collector`` directly). Normal operation never reads it.
        self._fallback_collector: Optional[TraceCollector] = None

    # ------------------------------------------------------------------
    # Per-run state access (resolved through the active _current_run)
    # ------------------------------------------------------------------

    def _data(self) -> Optional[Dict[str, Any]]:
        run = _current_run.get()
        return run.data if run is not None else None

    @property
    def _collector(self) -> Optional[TraceCollector]:
        run = _current_run.get()
        if run is not None:
            return run.collector
        return self._fallback_collector

    @_collector.setter
    def _collector(self, value: Optional[TraceCollector]) -> None:
        # Only used by the __new__-bypass cost test; real runs own a RunState.
        self._fallback_collector = value

    def _data_get(self, key: str, default: Any = None) -> Any:
        data = self._data()
        return data.get(key, default) if data is not None else default

    def _data_set(self, key: str, value: Any) -> None:
        data = self._data()
        if data is not None:
            data[key] = value

    @property
    def _crew_span_id(self) -> Optional[str]:
        return self._data_get("crew_span_id")

    @_crew_span_id.setter
    def _crew_span_id(self, value: Optional[str]) -> None:
        self._data_set("crew_span_id", value)

    @property
    def _current_task_span_id(self) -> Optional[str]:
        return self._data_get("current_task_span_id")

    @_current_task_span_id.setter
    def _current_task_span_id(self, value: Optional[str]) -> None:
        self._data_set("current_task_span_id", value)

    @property
    def _current_agent_span_id(self) -> Optional[str]:
        return self._data_get("current_agent_span_id")

    @_current_agent_span_id.setter
    def _current_agent_span_id(self, value: Optional[str]) -> None:
        self._data_set("current_agent_span_id", value)

    @property
    def _current_agent_role(self) -> Optional[str]:
        return self._data_get("current_agent_role")

    @_current_agent_role.setter
    def _current_agent_role(self, value: Optional[str]) -> None:
        self._data_set("current_agent_role", value)

    @property
    def _llm_in_flight_model(self) -> Optional[str]:
        return self._data_get("llm_in_flight_model")

    @_llm_in_flight_model.setter
    def _llm_in_flight_model(self, value: Optional[str]) -> None:
        self._data_set("llm_in_flight_model", value)

    @property
    def _delegation_seq(self) -> int:
        return self._data_get("delegation_seq", 0)

    @_delegation_seq.setter
    def _delegation_seq(self, value: int) -> None:
        self._data_set("delegation_seq", value)

    def _run_map(self, key: str) -> Dict[str, Any]:
        """Return a per-run dict (span-id maps / timers), or a throwaway when no run."""
        data = self._data()
        if data is None:
            return {}
        return data.setdefault(key, {})

    @property
    def _task_span_ids(self) -> Dict[str, str]:
        return self._run_map("task_span_ids")

    @property
    def _agent_span_ids(self) -> Dict[str, str]:
        return self._run_map("agent_span_ids")

    @property
    def _tool_span_ids(self) -> Dict[str, str]:
        return self._run_map("tool_span_ids")

    @property
    def _timers(self) -> Dict[str, int]:
        return self._run_map("timers")  # type: ignore[return-value]

    @property
    def _delegation_chain(self) -> List[Tuple[str, str]]:
        data = self._data()
        if data is None:
            return []
        return data.setdefault("delegation_chain", [])

    @staticmethod
    def _llm_timer_key(event: Any) -> str:
        """Stable timer key for an LLM call.

        Uses ``call_id`` when present (older crewai versions), otherwise
        falls back to ``agent_id``/``task_id`` (newer versions dropped
        ``call_id``). We deliberately keep a single key when none of these
        are present — LLM calls within a crew are serial, so the matching
        start/complete event pair shares the key.
        """
        call_id = getattr(event, "call_id", None)
        if call_id:
            return f"llm:{call_id}"
        agent_id = getattr(event, "agent_id", None)
        task_id = getattr(event, "task_id", None)
        if agent_id or task_id:
            return f"llm:{agent_id}:{task_id}"
        return "llm:current"

    # Run-opening events. Each maps to the handler that runs instead when the
    # event's lineage resolves to a run that is already open, so it becomes a
    # nested span of that run (LAY-4241). crewai >= 1.14.7 runs every agent
    # execution inside an internal ``AgentExecutor`` Flow; a crew or agent run
    # from a customer's own Flow method is the same shape. Treating those as new
    # runs split one execution across traces.
    _START_HANDLERS = {
        "_on_crew_started": "_on_nested_crew_started",
        "_on_flow_started": "_on_nested_flow_started",
    }

    # crewai scope-opening events the adapter does not render but must remember
    # for lineage, so whatever starts inside them still resolves to its run: a
    # crew kicked off from a Flow method has a MethodExecutionStartedEvent parent,
    # and ``Agent.kickoff()`` opens a LiteAgentExecutionStartedEvent scope.
    _LINEAGE_ONLY_EVENTS = (
        "MethodExecutionStartedEvent",
        "LiteAgentExecutionStartedEvent",
    )

    _EVENT_MAP = [
        ("CrewKickoffStartedEvent", "_on_crew_started"),
        ("CrewKickoffCompletedEvent", "_on_crew_completed"),
        ("CrewKickoffFailedEvent", "_on_crew_failed"),
        ("TaskStartedEvent", "_on_task_started"),
        ("TaskCompletedEvent", "_on_task_completed"),
        ("TaskFailedEvent", "_on_task_failed"),
        ("AgentExecutionStartedEvent", "_on_agent_execution_started"),
        ("AgentExecutionCompletedEvent", "_on_agent_execution_completed"),
        ("AgentExecutionErrorEvent", "_on_agent_execution_error"),
        ("LLMCallStartedEvent", "_on_llm_started"),
        ("LLMCallCompletedEvent", "_on_llm_completed"),
        ("LLMCallFailedEvent", "_on_llm_failed"),
        ("ToolUsageStartedEvent", "_on_tool_started"),
        ("ToolUsageFinishedEvent", "_on_tool_finished"),
        ("ToolUsageErrorEvent", "_on_tool_error"),
        ("FlowStartedEvent", "_on_flow_started"),
        ("FlowFinishedEvent", "_on_flow_finished"),
        ("MCPToolExecutionCompletedEvent", "_on_mcp_tool_completed"),
        ("MCPToolExecutionFailedEvent", "_on_mcp_tool_failed"),
    ]

    # Events only some crewai versions emit; subscribed when the class exists.
    # FlowFailedEvent arrives in 1.15: before it, a failed flow emits no end event,
    # so its run stays open until disconnect().
    _OPTIONAL_EVENT_MAP = [
        ("FlowFailedEvent", "_on_flow_failed"),
    ]

    # Optional delegation events — class names vary across crewai versions.
    # We attempt to subscribe to each at connect time, swallowing AttributeError
    # when the class doesn't exist in the installed version.
    _DELEGATION_EVENT_MAP = [
        ("AgentDelegationStartedEvent", "_on_delegation_started"),
        ("AgentDelegationCompletedEvent", "_on_delegation_completed"),
        ("DelegationEvent", "_on_delegation_started"),
    ]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _on_connect(self, target: Any = None, **kwargs: Any) -> None:
        self._check_dependency(_BaseEventListener is not None)
        self._subscribe()

    def _on_disconnect(self) -> None:
        self._unsubscribe()
        self._registered_handlers.clear()
        # Flush any runs still open (kickoff never completed before disconnect).
        with self._lock:
            runs = list(self._runs.values())
            self._runs.clear()
            self._event_parents.clear()
            self._event_root.clear()
        for run in runs:
            if run.data.get("borrowed_collector"):
                # Caller owns this collector; do not seal it on our disconnect.
                continue
            try:
                run.collector.flush()
            except Exception:
                log.warning("layerlens: error flushing open CrewAI run on disconnect", exc_info=True)
        self._fallback_collector = None

    def _subscribe(self) -> None:
        import crewai.events as ev  # pyright: ignore[reportMissingImports]

        # crewai >=1.x inspects the handler's param count and passes a
        # third `state` positional when there are 3 params, which would
        # silently clobber a default-arg closure. Bind via a factory so
        # the visible signature is exactly (source, event). The wrapper
        # binds the owning run's context before delegating (see _dispatch).
        def _make_handler(method_name):
            target = getattr(self, method_name)
            nested_name = self._START_HANDLERS.get(method_name)
            nested_target = getattr(self, nested_name) if nested_name else None

            def _handler(source: Any, event: Any) -> None:
                try:
                    self._dispatch(target, nested_target, source, event)
                except Exception:
                    log.warning("layerlens: error in CrewAI event handler", exc_info=True)

            return _handler

        def _lineage_handler(_source: Any, event: Any) -> None:
            try:
                self._record_lineage(event)
                # Ties the event to its run, so the run's end drops its lineage too.
                self._root_for(event)
            except Exception:
                log.warning("layerlens: error recording CrewAI event lineage", exc_info=True)

        for event_name, method_name in self._EVENT_MAP:
            event_cls = getattr(ev, event_name)
            handler = _make_handler(method_name)
            ev.crewai_event_bus.on(event_cls)(handler)
            self._registered_handlers.append((event_cls, handler))

        # Optional like the delegation events below: not every crewai version has them.
        for event_name in self._LINEAGE_ONLY_EVENTS:
            event_cls = getattr(ev, event_name, None)
            if event_cls is None:
                continue
            ev.crewai_event_bus.on(event_cls)(_lineage_handler)
            self._registered_handlers.append((event_cls, _lineage_handler))

        # Optional and delegation events: not every crewai version ships them.
        for event_name, method_name in self._OPTIONAL_EVENT_MAP + self._DELEGATION_EVENT_MAP:
            event_cls = getattr(ev, event_name, None)
            if event_cls is None:
                continue
            handler = _make_handler(method_name)
            ev.crewai_event_bus.on(event_cls)(handler)
            self._registered_handlers.append((event_cls, handler))

    def _unsubscribe(self) -> None:
        try:
            from crewai.events import (
                crewai_event_bus,
            )  # pyright: ignore[reportMissingImports]
        except ImportError:
            return
        for event_cls, handler in self._registered_handlers:
            try:
                off = getattr(crewai_event_bus, "off", None)
                if off is not None:
                    off(event_cls, handler)
                else:
                    # crewai < 1.14 has no off() API — remove our handler from
                    # the bus's internal registries directly so disconnect
                    # leaves no trace on every supported crewai version.
                    self._remove_handler_directly(crewai_event_bus, event_cls, handler)
            except Exception:
                log.debug(
                    "layerlens: could not unregister %s handler",
                    event_cls.__name__,
                    exc_info=True,
                )

    @staticmethod
    def _remove_handler_directly(bus: Any, event_cls: Any, handler: Any) -> None:
        for attr in ("_sync_handlers", "_async_handlers", "_handlers"):
            registry = getattr(bus, attr, None)
            if not isinstance(registry, dict) or event_cls not in registry:
                continue
            handlers = registry[event_cls]
            if handler not in handlers:
                continue
            if isinstance(handlers, frozenset):
                registry[event_cls] = handlers - {handler}
            elif isinstance(handlers, set):
                handlers.discard(handler)
            elif isinstance(handlers, list):
                registry[event_cls] = [h for h in handlers if h is not handler]

    # ------------------------------------------------------------------
    # Run resolution + dispatch
    # ------------------------------------------------------------------

    def _record_lineage(self, event: Any) -> None:
        """Remember child -> parent event ids so any event can find its root.

        Records the event's own ``parent_event_id`` and, from crewai's scope stack,
        every link of its ancestry. The stack makes lineage independent of handler
        order and of which scope events the adapter subscribes to, and it re-adds
        links the bounded map evicted while a long run was still open.
        """
        links: List[Tuple[str, Optional[str]]] = []
        eid = getattr(event, "event_id", None)
        if eid is not None:
            links.append((eid, getattr(event, "parent_event_id", None)))
        stack = _scope_stack_ids()
        for parent, child in zip(stack, stack[1:]):
            links.append((child, parent))
        with self._lock:
            for child, parent in links:
                if child not in self._event_parents:
                    self._event_parents[child] = parent
                    # Bound the map: only orphan events are left in it once their
                    # run ends (_end_trace drops a run's lineage).
                    if len(self._event_parents) > _MAX_TRACKED_EVENTS:
                        self._event_parents.pop(next(iter(self._event_parents)), None)

    def _root_for(self, event: Any) -> Optional[str]:
        """Walk the event's lineage back to a registered run's root event_id.

        An end event whose ``started_event_id`` opened a run belongs to that run:
        crewai stamps a scope-ENDING event with its enclosing scope as parent, so
        a top-level kickoff's ``CrewKickoffCompletedEvent`` has
        ``parent_event_id=None`` and only ``started_event_id`` ties it to the run.
        Every other event follows the ``parent_event_id`` chain.

        Returns the owning root event_id when found in ``self._runs``, else None.
        """
        eid = getattr(event, "event_id", None)
        started = getattr(event, "started_event_id", None)
        with self._lock:
            # Memoised result.
            cached = self._event_root.get(eid) if eid is not None else None
            if cached is not None and cached in self._runs:
                return cached
            if started is not None and started in self._runs:
                self._memoise_root(eid, started)
                return started
            cur = eid
            seen = 0
            while cur is not None and seen < _MAX_TRACKED_EVENTS:
                if cur in self._runs:
                    self._memoise_root(eid, cur)
                    return cur
                cur = self._event_parents.get(cur)
                seen += 1
        return None

    def _memoise_root(self, eid: Optional[str], root: str) -> None:
        """Cache *eid*'s run root (caller holds ``self._lock``). Bounded like the
        lineage map; ``_end_trace`` drops a run's entries when it ends."""
        if eid is None:
            return
        self._event_root[eid] = root
        if len(self._event_root) > _MAX_TRACKED_EVENTS:
            self._event_root.pop(next(iter(self._event_root)), None)

    def _resolve_run(self, event: Any) -> Optional[RunState]:
        """Find the RunState owning *event* (by lineage), or the lone active run.

        Single-run drivers (and bare-constructed test events with no
        ``parent_event_id`` linkage) fall back to the only open run, which is
        unambiguous when exactly one run is in flight.
        """
        root = self._root_for(event)
        with self._lock:
            if root is not None:
                return self._runs.get(root)
            if len(self._runs) == 1:
                return next(iter(self._runs.values()))
        return None

    def _dispatch(self, target: Any, nested_target: Any, source: Any, event: Any) -> None:
        """Bind the owning run's context, then run the real handler.

        ``nested_target`` is set only for run-opening events (crew / flow start).
        Such an event opens a new run unless its lineage resolves to a run that
        is already open, in which case ``nested_target`` records it as a nested
        span of that run. Lineage alone decides: the single-open-run fallback
        would fold an unrelated concurrent kickoff into whatever run is open.
        """
        self._record_lineage(event)
        if nested_target is not None:
            root = self._root_for(event)
            with self._lock:
                run = self._runs.get(root) if root is not None else None
            if run is None:
                # Opens, and binds, its own run.
                target(source, event)
                return
            target = nested_target
        else:
            run = self._resolve_run(event)
            if run is None:
                # No owning run (completion with no start, or post-flush stray).
                return
        run_token = _current_run.set(run)
        col_token = _current_collector.set(run.collector)
        try:
            target(source, event)
        finally:
            _current_collector.reset(col_token)
            _current_run.reset(run_token)

    def _begin_crew_run(self, event: Any, span_id: str, kind: str) -> None:
        """Open a per-run collector keyed by this kickoff's root event_id.

        Binds ``_current_run`` / ``_current_collector`` for the rest of THIS
        handler so the start event emits into the new run; subsequent events of
        the same kickoff re-resolve the run by lineage in ``_dispatch``.

        If the caller already bound a collector (e.g. inside ``trace_context``
        or a harness that set ``_current_collector`` before ``kickoff``), reuse
        it instead of minting a fresh one — mirroring ``FrameworkAdapter._begin_run``.
        CrewAI 1.x dispatches every handler inside a ``contextvars.copy_context()``
        on a worker thread, and that copy carries the caller's ``_current_collector``
        binding into the handler, so ``.get()`` here sees it. Without this reuse,
        events land in an internal collector that the caller never sees (the
        "captures 0 events" symptom). A borrowed collector is NOT flushed on
        ``_end_trace`` — its owner controls its lifecycle.
        """
        existing = _current_collector.get()
        if existing is not None:
            collector = existing
            borrowed = True
        else:
            collector = TraceCollector(self._client, self._config)
            borrowed = False
        root_id = getattr(event, "event_id", None) or self._new_span_id()
        run = RunState(
            collector=collector,
            root_span_id=span_id,
            data={
                "crew_span_id": span_id,
                "root_id": root_id,
                # "crew" or "flow": which kind of end event may close the run.
                "root_kind": kind,
                "borrowed_collector": borrowed,
                "event_spans": {root_id: span_id},
                # Spans of the crews in this run, and those whose model calls
                # emitted their own cost.record (see _on_crew_completed).
                "crew_spans": {span_id} if kind == "crew" else set(),
                "crews_with_call_costs": set(),
            },
        )
        with self._lock:
            self._runs[root_id] = run
            self._memoise_root(root_id, root_id)
        with self._spans_recorded:
            self._spans_recorded.notify_all()
        run._col_token = _current_collector.set(collector)
        run._token = _current_run.set(run)

    def _fire(
        self,
        event_type: str,
        payload: Dict[str, Any],
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        span_name: Optional[str] = None,
    ) -> None:
        """Emit directly to the current run's collector."""
        c = self._collector
        if c is None:
            return
        if event_type == "cost.record" and payload.get("cost_usd") is None:
            self._price_cost_record(payload)
        c.emit(
            event_type,
            payload,
            span_id=span_id or self._new_span_id(),
            parent_span_id=parent_span_id,
            span_name=span_name,
        )

    def _remember_span(self, event: Any, span_id: str) -> None:
        """Record the span opened for *event* so its descendants can find it."""
        eid = getattr(event, "event_id", None)
        if eid is not None:
            spans = self._run_map("event_spans")
            with self._spans_recorded:
                spans[eid] = span_id
                self._spans_recorded.notify_all()

    def _closed_span(self, by_label: Dict[str, str], label: str, event: Any, fallback: Optional[str]) -> str:
        """The span an end event closes: the one its own start opened, paired
        through ``started_event_id`` (exact even when concurrent crews in one run
        reuse a role or task label), else the span last opened under ``label``."""
        started = getattr(event, "started_event_id", None)
        spans = self._data_get("event_spans") or {}
        span = spans.get(started) if started is not None else None
        if span is None:
            return by_label.pop(label, None) or fallback or self._new_span_id()
        if by_label.get(label) == span:
            by_label.pop(label, None)
        return span

    def _ancestor_span(self, event: Any) -> Optional[str]:
        """The span this run opened for *event*'s nearest ancestor, by lineage.

        crewai's lineage is the real nesting. Inside the 1.14.7+ AgentExecutor
        flow an LLM call's parent is that flow, not the agent, and a crew run
        from a Flow method sits under the flow. None when the event carries no
        lineage this run knows (pre-1.14 crewai, or a bare event); callers fall
        back to the tracked current task / agent.
        """
        return next(self._ancestor_spans(event), None)

    def _ancestor_spans(self, event: Any) -> Iterator[str]:
        """The spans this run opened for *event*'s ancestors, nearest first."""
        spans = self._data_get("event_spans")
        if not spans or event is None:
            return
        cur = getattr(event, "parent_event_id", None)
        chain: List[str] = []
        with self._lock:
            while cur is not None and len(chain) < _MAX_TRACKED_EVENTS:
                chain.append(cur)
                cur = self._event_parents.get(cur)
        # The ancestors whose handler records a span, as crewai's scope stack names them: one
        # whose span is missing yet is still being handled on another thread.
        span_scopes = {event_id for event_id, kind in _scope_stack() if kind in _SPAN_SCOPES}
        for event_id in chain:
            span = spans.get(event_id)
            if span is None and event_id in span_scopes:
                span = self._wait_for_span(spans, event_id)
            if span is not None:
                yield span

    def _wait_for_span(self, spans: Dict[str, str], event_id: str) -> Optional[str]:
        """The span recorded for *event_id* once its handler has run, or None after a short wait.

        Never called under ``self._lock``: the handler being waited for takes it.
        """
        deadline = time.monotonic() + _ANCESTOR_SPAN_WAIT_SECONDS
        with self._spans_recorded:
            while event_id not in spans:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._spans_recorded.wait(left)
            return spans[event_id]

    def _owning_crew_span(self, event: Any) -> Optional[str]:
        """The span of the crew *event* runs in: its nearest crew ancestor, or, with
        no lineage, the run's own crew."""
        crews = self._data_get("crew_spans") or set()
        for span in self._ancestor_spans(event):
            if span in crews:
                return span
        return self._crew_span_id if self._data_get("root_kind") == "crew" else None

    def _leaf_parent(self, event: Any = None) -> Optional[str]:
        return (
            self._ancestor_span(event)
            or self._current_agent_span_id
            or self._current_task_span_id
            or self._crew_span_id
        )

    def _open_nested(self, event: Any, kind: str, name: str, payload: Dict[str, Any], span_name: str) -> str:
        """Emit the input of a crew / flow that started inside this run, as a span
        under its lineage parent. Only the event that opened the run ends it; this
        span is closed by the matching end event (see :meth:`_closing`)."""
        eid = getattr(event, "event_id", None)
        span_id = self._new_span_id()
        parent = self._leaf_parent(event)
        self._remember_span(event, span_id)
        self._run_map("nested_spans")[eid] = {"span_id": span_id, "parent_span_id": parent, "kind": kind, "name": name}
        self._tick(f"nested:{eid}")
        self._fire("agent.input", payload, span_id=span_id, parent_span_id=parent, span_name=span_name)
        return span_id

    def _closing(self, event: Any, kind: str, name: str) -> Tuple[str, Optional[str], Optional[str], Optional[float]]:
        """What a crew / flow end event closes: ``("run", root span, None, latency)``,
        ``("nested", span, parent span, latency)``, or ``("foreign", None, None, None)``
        for the end of a span this run never opened.

        crewai pairs an end event with its start through ``started_event_id``.
        When that names neither the run nor a nested span of the same kind, there
        was no pairing (pre-1.14 crewai, a bare event) or crewai's scope stack was
        unbalanced: an end event pops whatever scope is on top, with an "Event
        pairing mismatch" warning, so ``started_event_id`` names some other scope
        (possibly the run's own root). Fall back to the most recently opened span
        of the same kind and name, then to the run itself when a start of this
        kind opened it.
        """
        nested = self._run_map("nested_spans")
        started = getattr(event, "started_event_id", None)
        opened_run = self._data_get("root_kind") == kind
        key: Optional[str] = None
        if started is not None and started in nested and nested[started]["kind"] == kind:
            key = started
        elif not (started is not None and started == self._data_get("root_id") and opened_run):
            key = next(
                (k for k in reversed(list(nested)) if nested[k]["kind"] == kind and nested[k]["name"] == name),
                None,
            )
            if key is None and not opened_run:
                log.debug("layerlens: ignoring CrewAI %s for a span this run did not open", type(event).__name__)
                return "foreign", None, None, None
        if key is not None:
            span = nested.pop(key)
            return "nested", span["span_id"], span["parent_span_id"], self._tock(f"nested:{key}")
        return "run", self._crew_span_id or self._new_span_id(), None, self._tock("crew")

    def _tick(self, key: str) -> None:
        self._timers[key] = time.time_ns()

    def _tock(self, key: str) -> Optional[float]:
        start = self._timers.pop(key, 0)
        if not start:
            return None
        return (time.time_ns() - start) / 1_000_000

    def _end_trace(self) -> None:
        """Flush and tear down the current run, restoring the prior context."""
        run = _current_run.get()
        if run is None:
            return
        root_id = run.data.get("root_id")
        with self._lock:
            if root_id is not None:
                self._runs.pop(root_id, None)
                # Drop the run's lineage: every event resolved to it is memoised here.
                ended = [eid for eid, root in self._event_root.items() if root == root_id]
                for eid in ended:
                    self._event_root.pop(eid, None)
                    self._event_parents.pop(eid, None)
        collector = run.collector
        if run._col_token is not None:
            try:
                _current_collector.reset(run._col_token)
            except ValueError:
                _current_collector.set(None)
            run._col_token = None
        if run._token is not None:
            try:
                _current_run.reset(run._token)
            except ValueError:
                _current_run.set(None)
            run._token = None
        # A borrowed collector belongs to the caller (trace_context / harness);
        # flushing it here would seal it before the caller is done. Only flush
        # collectors this run created.
        if not run.data.get("borrowed_collector"):
            collector.flush()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_name(obj: Any) -> str:
        return getattr(obj, "name", None) or type(obj).__name__

    @staticmethod
    def _get_task_name(event: Any) -> str:
        """The task label. crewai does NOT guarantee this is a short name — for an
        unnamed ``Task(description=...)`` it populates ``event.task_name`` with the
        full free-text DESCRIPTION. So this is CONTENT, not a safe identifier: use
        it as the internal correlation key (never uploaded raw) and only surface it
        on the wire when ``capture_content`` is on (see :meth:`_fill_task_fields` /
        :meth:`_span_label`). F-CREWAI: it previously shipped in the clear under the
        privacy default. crewai identity comes from ``crew_name``, not this label."""
        name = getattr(event, "task_name", None)
        if name:
            return str(name)
        task = getattr(event, "task", None)
        if task:
            return str(getattr(task, "description", None) or getattr(task, "name", ""))[:200]
        return ""

    @staticmethod
    def _tool_key(event: Any) -> str:
        tool_name = getattr(event, "tool_name", None) or ""
        agent_key = getattr(event, "agent_key", None) or ""
        return f"{tool_name}:{agent_key}"

    # ------------------------------------------------------------------
    # Crew lifecycle
    # ------------------------------------------------------------------

    def _crew_input(self, source: Any, event: Any) -> Tuple[str, Dict[str, Any]]:
        crew_name = getattr(event, "crew_name", None) or self._get_name(source)
        payload = self._payload(crew_name=crew_name)
        self._set_if_capturing(payload, "input", safe_serialize(getattr(event, "inputs", None)))
        return crew_name, payload

    def _on_crew_started(self, source: Any, event: Any) -> None:
        span_id = self._new_span_id()
        self._begin_crew_run(event, span_id, "crew")
        self._tick("crew")
        crew_name, payload = self._crew_input(source, event)
        self._fire(
            "agent.input",
            payload,
            span_id=span_id,
            parent_span_id=None,
            span_name=crew_name,
        )

    def _on_nested_crew_started(self, source: Any, event: Any) -> None:
        crew_name, payload = self._crew_input(source, event)
        span_id = self._open_nested(event, "crew", crew_name, payload, span_name=crew_name)
        crews = self._data_get("crew_spans")
        if crews is not None:
            crews.add(span_id)

    def _on_crew_completed(self, source: Any, event: Any) -> None:
        crew_name = getattr(event, "crew_name", None) or self._get_name(source)
        closes, span_id, parent, latency_ms = self._closing(event, "crew", crew_name)
        if closes == "foreign":
            return
        payload = self._payload(crew_name=crew_name)
        if latency_ms is not None:
            payload["duration_ns"] = int(latency_ms * 1_000_000)
        self._set_if_capturing(payload, "output", safe_serialize(getattr(event, "output", None)))
        total_tokens = getattr(event, "total_tokens", None)
        if total_tokens is not None:
            payload["tokens_total"] = total_tokens
        self._fire(
            "agent.output",
            payload,
            span_id=span_id,
            parent_span_id=parent,
            span_name=crew_name,
        )
        # crewai's aggregate is a cost.record only when the crew's model calls
        # didn't emit their own: the same tokens must not be counted twice.
        if total_tokens and span_id not in (self._data_get("crews_with_call_costs") or set()):
            self._fire(
                "cost.record",
                self._payload(tokens_total=total_tokens),
                span_id=span_id,
                parent_span_id=parent,
            )
        if closes == "run":
            self._end_trace()

    def _on_crew_failed(self, source: Any, event: Any) -> None:
        crew_name = getattr(event, "crew_name", None) or self._get_name(source)
        closes, span_id, parent, _ = self._closing(event, "crew", crew_name)
        if closes == "foreign":
            return
        error = str(getattr(event, "error", "unknown error"))
        self._fire(
            "agent.error",
            self._payload(crew_name=crew_name, error=error, error_type="crew_error", status="error"),
            span_id=span_id,
            parent_span_id=parent,
            span_name=crew_name,
        )
        if closes == "run":
            self._end_trace()

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def _fill_task_fields(self, payload: Dict[str, Any], event: Any) -> None:
        """Attach the task label as content-gated ``task_name`` — withheld under
        capture_content=False (F-CREWAI: crewai puts the free-text description in
        this field for unnamed tasks, so it is content, not a safe identifier)."""
        self._set_if_capturing(payload, "task_name", self._get_task_name(event) or None)

    def _span_label(self, event: Any) -> str:
        """Task span label. The task name/description is content, so only surface
        it when capturing; otherwise a neutral ``task`` (never leak the description
        via the span_name envelope, which redaction does not strip)."""
        if self._config.capture_content:
            name = self._get_task_name(event)
            return f"task:{name[:60]}" if name else "task"
        return "task"

    def _on_task_started(self, source: Any, event: Any) -> None:
        task_name = self._get_task_name(event)
        span_id = self._new_span_id()
        self._task_span_ids[task_name] = span_id
        self._current_task_span_id = span_id
        self._remember_span(event, span_id)
        parent = self._ancestor_span(event) or self._crew_span_id
        agent_role = getattr(event, "agent_role", None)
        payload = self._payload()
        self._fill_task_fields(payload, event)
        if agent_role:
            payload["agent_role"] = agent_role
        if self._config.capture_content:
            context = getattr(event, "context", None)
            if context:
                payload["context"] = str(context)[:500]
        self._fire(
            "agent.input",
            payload,
            span_id=span_id,
            parent_span_id=parent,
            span_name=self._span_label(event),
        )

    def _on_task_completed(self, source: Any, event: Any) -> None:
        task_name = self._get_task_name(event)
        span_id = self._closed_span(self._task_span_ids, task_name, event, self._current_task_span_id)
        parent = self._ancestor_span(event) or self._crew_span_id
        payload = self._payload()
        self._fill_task_fields(payload, event)
        self._set_if_capturing(payload, "output", safe_serialize(getattr(event, "output", None)))
        self._fire(
            "agent.output",
            payload,
            span_id=span_id,
            parent_span_id=parent,
            span_name=self._span_label(event),
        )

    def _on_task_failed(self, source: Any, event: Any) -> None:
        task_name = self._get_task_name(event)
        span_id = self._closed_span(self._task_span_ids, task_name, event, self._current_task_span_id)
        parent = self._ancestor_span(event) or self._crew_span_id
        payload = self._payload(
            error=str(getattr(event, "error", "unknown error")),
            error_type="task_error",
            status="error",
        )
        self._fill_task_fields(payload, event)
        self._fire(
            "agent.error",
            payload,
            span_id=span_id,
            parent_span_id=parent,
            span_name=self._span_label(event),
        )

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    def _on_agent_execution_started(self, source: Any, event: Any) -> None:
        agent = getattr(event, "agent", None)
        agent_role = (
            getattr(event, "agent_role", None) or (getattr(agent, "role", None) if agent else None) or "unknown"
        )
        span_id = self._new_span_id()
        self._agent_span_ids[agent_role] = span_id
        self._current_agent_span_id = span_id
        self._current_agent_role = agent_role
        self._remember_span(event, span_id)
        parent = self._ancestor_span(event) or self._current_task_span_id or self._crew_span_id
        payload = self._payload(agent_role=agent_role)
        # Capture manager-agent context so hierarchical crews are visible.
        allow_delegation = getattr(agent, "allow_delegation", None) if agent else None
        if allow_delegation is not None:
            payload["allow_delegation"] = bool(allow_delegation)
        is_manager = getattr(agent, "is_manager", None) if agent else None
        if is_manager is not None:
            payload["is_manager"] = bool(is_manager)
        tools = getattr(event, "tools", None)
        if tools:
            payload["tools"] = [getattr(t, "name", str(t)) for t in tools]
        if self._config.capture_content:
            task_prompt = getattr(event, "task_prompt", None)
            if task_prompt:
                payload["task_prompt"] = str(task_prompt)[:500]
        self._fire(
            "agent.input",
            payload,
            span_id=span_id,
            parent_span_id=parent,
            span_name=f"agent:{agent_role[:60]}",
        )

    def _on_agent_execution_completed(self, source: Any, event: Any) -> None:
        agent = getattr(event, "agent", None)
        agent_role = (
            getattr(event, "agent_role", None) or (getattr(agent, "role", None) if agent else None) or "unknown"
        )
        span_id = self._closed_span(self._agent_span_ids, agent_role, event, self._current_agent_span_id)
        parent = self._ancestor_span(event) or self._current_task_span_id or self._crew_span_id
        if self._current_agent_span_id == span_id:
            self._current_agent_span_id = None
        payload = self._payload(agent_role=agent_role, status="ok")
        self._set_if_capturing(payload, "output", safe_serialize(getattr(event, "output", None)))
        self._fire(
            "agent.output",
            payload,
            span_id=span_id,
            parent_span_id=parent,
            span_name=f"agent:{agent_role[:60]}",
        )

    def _on_agent_execution_error(self, source: Any, event: Any) -> None:
        agent = getattr(event, "agent", None)
        agent_role = (
            getattr(event, "agent_role", None) or (getattr(agent, "role", None) if agent else None) or "unknown"
        )
        error = str(getattr(event, "error", "unknown error"))
        span_id = self._closed_span(self._agent_span_ids, agent_role, event, self._current_agent_span_id)
        parent = self._ancestor_span(event) or self._current_task_span_id or self._crew_span_id
        if self._current_agent_span_id == span_id:
            self._current_agent_span_id = None
        self._fire(
            "agent.error",
            self._payload(agent_role=agent_role, error=error, error_type="agent_error", status="error"),
            span_id=span_id,
            parent_span_id=parent,
            span_name=f"agent:{agent_role[:60]}",
        )

    # ------------------------------------------------------------------
    # Delegation / handoff (hierarchical crews)
    # ------------------------------------------------------------------

    def _next_delegation_seq(self, from_agent: str, to_agent: str) -> int:
        """Bump the per-run delegation counter and record the (from, to) pair."""
        data = self._data()
        if data is None:
            return 1
        seq = data.get("delegation_seq", 0) + 1
        data["delegation_seq"] = seq
        data.setdefault("delegation_chain", []).append((from_agent, to_agent))
        return seq

    @staticmethod
    def _extract_delegation_args(tool_args: Any) -> Dict[str, Any]:
        """Pull ``task`` / ``context`` / ``coworker`` out of whatever crewai passed in.

        ``tool_args`` may be a dict, a JSON-encoded string, or ``None``.
        """
        if tool_args is None:
            return {}
        if isinstance(tool_args, dict):
            return tool_args
        if isinstance(tool_args, str):
            import json

            try:
                parsed = json.loads(tool_args)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, TypeError):
                pass
        return {}

    def _emit_delegation_from_tool(self, event: Any, tool_name: str, tool_span_id: str) -> None:
        """Emit ``agent.handoff`` for a built-in coworker-delegation tool call.

        Bridges the gap between crewai versions: newer versions fire
        ``AgentDelegationStartedEvent`` which we handle below; older
        versions only emit the tool call, so we synthesize the handoff
        from the tool args.
        """
        tool_args = self._extract_delegation_args(getattr(event, "tool_args", None))
        coworker = tool_args.get("coworker")
        # The coworker is the delegation TARGET; without it there is no real
        # handoff to draw — omit rather than fabricate "unknown" (F9).
        to_agent = str(coworker) if coworker else None
        if to_agent is None:
            return
        # _current_agent_role is the honest tracked role; its
        # _on_agent_execution_started fallback is the literal "unknown", which is
        # not a real endpoint — treat it as absent (honest blank).
        from_agent = self._current_agent_role
        if not from_agent or from_agent == "unknown":
            from_agent = None
        seq = self._next_delegation_seq(from_agent or "", to_agent)

        summary = scrub_context(
            {
                "task": tool_args.get("task"),
                "context": tool_args.get("context"),
            }
        )
        payload = self._payload(
            to_agent=to_agent,
            reason="delegation",
            delegation_seq=seq,
            tool_name=tool_name,
        )
        if from_agent is not None:
            payload["from_agent"] = from_agent
        if summary:
            try:
                payload["handoff_context_hash"] = compute_hash(summary)
            except TypeError:
                payload["handoff_context_hash"] = compute_hash({"_repr": repr(summary)})
            if self._config.capture_content:
                payload["context"] = summary
        self._fire("agent.handoff", payload, parent_span_id=tool_span_id)

    def _on_delegation_started(self, source: Any, event: Any) -> None:
        from_role = (
            getattr(event, "from_agent", None)
            or getattr(event, "manager_role", None)
            or getattr(event, "source_agent", None)
        )
        to_role = (
            getattr(event, "to_agent", None)
            or getattr(event, "delegate_role", None)
            or getattr(event, "target_agent", None)
        )
        # No honest endpoint on either side -> an edge with no ends is not drawn:
        # omit rather than fabricate "manager"/"worker" (F9).
        if from_role is None and to_role is None:
            return
        seq = self._next_delegation_seq(str(from_role or ""), str(to_role or ""))
        task_name = self._get_task_name(event) or getattr(event, "description", "") or ""
        payload = self._payload(phase="start", reason="delegation", delegation_seq=seq)
        if from_role is not None:
            payload["from_agent"] = str(from_role)
        if to_role is not None:
            payload["to_agent"] = str(to_role)
        if task_name:
            payload["task"] = str(task_name)[:200]
        self._set_if_capturing(payload, "context", safe_serialize(getattr(event, "context", None)))
        self._fire("agent.handoff", payload, parent_span_id=self._leaf_parent(event))

    def _on_delegation_completed(self, source: Any, event: Any) -> None:
        from_role = getattr(event, "from_agent", None) or getattr(event, "manager_role", None)
        to_role = getattr(event, "to_agent", None) or getattr(event, "delegate_role", None)
        if from_role is None and to_role is None:
            return
        payload = self._payload(phase="complete")
        if from_role is not None:
            payload["from_agent"] = str(from_role)
        if to_role is not None:
            payload["to_agent"] = str(to_role)
        self._set_if_capturing(payload, "result", safe_serialize(getattr(event, "result", None)))
        self._fire("agent.handoff", payload, parent_span_id=self._leaf_parent(event))

    # ------------------------------------------------------------------
    # LLM calls
    # ------------------------------------------------------------------

    def _on_llm_started(self, source: Any, event: Any) -> None:
        key = self._llm_timer_key(event)
        self._tick(key)
        # Remember the model for the paired completed/failed event, which in
        # newer crewai drops ``call_id`` and may also drop ``model`` on failure.
        self._llm_in_flight_model = getattr(event, "model", None)

    def _on_llm_completed(self, source: Any, event: Any) -> None:
        model = getattr(event, "model", None) or self._llm_in_flight_model
        # crewai 1.x reports the call's usage on the event (its native providers
        # pass ``response`` as the bare assistant text); older shapes nest it in
        # the response object or dict.
        usage = getattr(event, "usage", None)
        if not usage:
            response = getattr(event, "response", None)
            usage = (
                getattr(response, "usage", None)
                if response and not isinstance(response, dict)
                else (response.get("usage") if isinstance(response, dict) else None)
            )
        tokens = self._normalize_tokens(usage)
        # The agent crewai says made the call, so its usage and cost can be
        # attributed to that agent.
        agent_role = getattr(event, "agent_role", None)
        payload = self._payload()
        if model:
            payload["model"] = model
        if agent_role:
            payload["agent_role"] = agent_role
        key = self._llm_timer_key(event)
        latency_ms = self._tock(key)
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms
        payload.update(tokens)
        parent = self._leaf_parent(event)
        span_id = self._new_span_id()
        self._fire("model.invoke", payload, span_id=span_id, parent_span_id=parent)
        if tokens:
            cost_payload = self._payload(model=model, **tokens)
            if agent_role:
                cost_payload["agent_role"] = agent_role
            self._fire("cost.record", cost_payload, span_id=span_id, parent_span_id=parent)
            crew = self._owning_crew_span(event)
            priced = self._data_get("crews_with_call_costs")
            if crew is not None and priced is not None:
                priced.add(crew)
        self._llm_in_flight_model = None

    def _on_llm_failed(self, source: Any, event: Any) -> None:
        error = str(getattr(event, "error", "unknown error"))
        model = getattr(event, "model", None) or self._llm_in_flight_model
        payload = self._payload(error=error, error_type="llm_error", status="error")
        if model:
            payload["model"] = model
        agent_role = getattr(event, "agent_role", None)
        if agent_role:
            payload["agent_role"] = agent_role
        self._fire("agent.error", payload, parent_span_id=self._leaf_parent(event))

    # ------------------------------------------------------------------
    # Tool usage
    # ------------------------------------------------------------------

    def _on_tool_started(self, source: Any, event: Any) -> None:
        tool_name = getattr(event, "tool_name", None) or "unknown"
        span_id = self._new_span_id()
        key = self._tool_key(event)
        self._tool_span_ids[key] = span_id
        self._remember_span(event, span_id)
        payload = self._payload(tool_name=tool_name)
        self._set_if_capturing(payload, "input", safe_serialize(getattr(event, "tool_args", None)))
        self._fire("tool.call", payload, span_id=span_id, parent_span_id=self._leaf_parent(event))

        # Detect delegation invoked via the built-in coworker tools — older
        # crewai versions don't fire typed delegation events, so without this
        # the handoff is invisible in the trace.
        if _is_delegation_tool(tool_name):
            self._emit_delegation_from_tool(event, tool_name, span_id)

    def _on_tool_finished(self, source: Any, event: Any) -> None:
        tool_name = getattr(event, "tool_name", None) or "unknown"
        key = self._tool_key(event)
        span_id = self._closed_span(self._tool_span_ids, key, event, None)
        payload = self._payload(tool_name=tool_name)
        self._set_if_capturing(payload, "output", safe_serialize(getattr(event, "output", None)))
        started_at = getattr(event, "started_at", None)
        finished_at = getattr(event, "finished_at", None)
        if started_at is not None and finished_at is not None:
            try:
                payload["latency_ms"] = (finished_at - started_at).total_seconds() * 1000
            except Exception:
                pass
        if getattr(event, "from_cache", None):
            payload["from_cache"] = True
        self._fire("tool.result", payload, span_id=span_id, parent_span_id=self._leaf_parent(event))

    def _on_tool_error(self, source: Any, event: Any) -> None:
        tool_name = getattr(event, "tool_name", None) or "unknown"
        error = str(getattr(event, "error", "unknown error"))
        key = self._tool_key(event)
        self._tool_span_ids.pop(key, None)
        self._fire(
            "agent.error",
            self._payload(tool_name=tool_name, error=error, error_type="tool_error", status="error"),
            parent_span_id=self._leaf_parent(event),
        )

    # ------------------------------------------------------------------
    # Flow events
    # ------------------------------------------------------------------

    def _flow_input(self, source: Any, event: Any) -> Tuple[str, Dict[str, Any]]:
        flow_name = getattr(event, "flow_name", None) or self._get_name(source)
        payload = self._payload(flow_name=flow_name)
        self._set_if_capturing(payload, "input", safe_serialize(getattr(event, "inputs", None)))
        return flow_name, payload

    def _on_flow_started(self, source: Any, event: Any) -> None:
        span_id = self._new_span_id()
        self._begin_crew_run(event, span_id, "flow")
        self._tick("crew")
        flow_name, payload = self._flow_input(source, event)
        self._fire(
            "agent.input",
            payload,
            span_id=span_id,
            parent_span_id=None,
            span_name=f"flow:{flow_name}",
        )

    def _on_nested_flow_started(self, source: Any, event: Any) -> None:
        flow_name, payload = self._flow_input(source, event)
        self._open_nested(event, "flow", flow_name, payload, span_name=f"flow:{flow_name}")

    def _on_flow_finished(self, source: Any, event: Any) -> None:
        flow_name = getattr(event, "flow_name", None) or self._get_name(source)
        closes, span_id, parent, latency_ms = self._closing(event, "flow", flow_name)
        if closes == "foreign":
            return
        payload = self._payload(flow_name=flow_name)
        if latency_ms is not None:
            payload["duration_ns"] = int(latency_ms * 1_000_000)
        self._set_if_capturing(payload, "output", safe_serialize(getattr(event, "result", None)))
        self._fire(
            "agent.output",
            payload,
            span_id=span_id,
            parent_span_id=parent,
            span_name=f"flow:{flow_name}",
        )
        if closes == "run":
            self._end_trace()

    def _on_flow_failed(self, source: Any, event: Any) -> None:
        flow_name = getattr(event, "flow_name", None) or self._get_name(source)
        closes, span_id, parent, _ = self._closing(event, "flow", flow_name)
        if closes == "foreign":
            return
        error = str(getattr(event, "error", "unknown error"))
        self._fire(
            "agent.error",
            self._payload(flow_name=flow_name, error=error, error_type="flow_error", status="error"),
            span_id=span_id,
            parent_span_id=parent,
            span_name=f"flow:{flow_name}",
        )
        if closes == "run":
            self._end_trace()

    # ------------------------------------------------------------------
    # MCP tool events
    # ------------------------------------------------------------------

    def _on_mcp_tool_completed(self, source: Any, event: Any) -> None:
        tool_name = getattr(event, "tool_name", None) or "unknown"
        server_name = getattr(event, "server_name", None)
        latency_ms = getattr(event, "execution_duration_ms", None)
        payload = self._payload(tool_name=tool_name)
        self._set_if_capturing(payload, "output", safe_serialize(getattr(event, "result", None)))
        if server_name:
            payload["mcp_server"] = server_name
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms
        self._fire("tool.call", payload, parent_span_id=self._leaf_parent(event))

    def _on_mcp_tool_failed(self, source: Any, event: Any) -> None:
        tool_name = getattr(event, "tool_name", None) or "unknown"
        error = str(getattr(event, "error", "unknown error"))
        server_name = getattr(event, "server_name", None)
        payload = self._payload(tool_name=tool_name, error=error)
        payload["error_type"] = "mcp_tool_error"
        payload["status"] = "error"
        if server_name:
            payload["mcp_server"] = server_name
        self._fire("agent.error", payload, parent_span_id=self._leaf_parent(event))
