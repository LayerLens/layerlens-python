from __future__ import annotations

import logging
import weakref
from typing import Any, Dict, List, Tuple, Optional

from ._utils import safe_serialize
from ..._identity import _API_METHOD_RE, _s, _is_generic
from ._base_framework import FrameworkAdapter
from ..._capture_config import CaptureConfig
from ..providers.pricing import BEDROCK_PRICING

#: The strands built-in swarm handoff tool. When a developer's agent calls
#: ``handoff_to_agent(agent_name, message)`` (see
#: ``strands.multiagent.swarm.Swarm._create_handoff_tool``) it is a
#: producer-DECLARED transition to another named agent — the honest signal for
#: an ``agent.handoff`` graph edge.
_HANDOFF_TOOL = "handoff_to_agent"

log = logging.getLogger(__name__)

_HAS_STRANDS = False
try:
    from strands.hooks.events import (  # pyright: ignore[reportMissingImports]
        AfterToolCallEvent as _AfterToolCallEvent,
        AfterModelCallEvent as _AfterModelCallEvent,
        BeforeToolCallEvent as _BeforeToolCallEvent,
        AfterInvocationEvent as _AfterInvocationEvent,
        BeforeModelCallEvent as _BeforeModelCallEvent,
        AgentInitializedEvent as _AgentInitializedEvent,
        BeforeInvocationEvent as _BeforeInvocationEvent,
    )

    _HAS_STRANDS = True
except ImportError:
    pass


class StrandsAdapter(FrameworkAdapter):
    """AWS Strands Agents adapter using the native hook system.

    Implements ``HookProvider`` and registers for all lifecycle events:
    agent init, invocation start/end, model calls, and tool calls.

    Usage::

        adapter = StrandsAdapter(client)
        adapter.connect()

        # Pass the adapter as a hook provider at construction:
        agent = Agent(model=model, hooks=[adapter])
        result = agent("Hello!")

        # Or register on an existing agent:
        adapter.connect(target=agent)
        result = agent("Hello!")

        adapter.disconnect()

    The adapter traces only while connected. ``disconnect()`` stops new runs
    at once; a run already in progress finishes and uploads, and then the
    adapter's hooks leave its agent. Agents with no run in progress lose them
    straight away, whichever way the adapter was attached. To trace an agent
    again afterwards, attach the adapter to it again.
    """

    name = "strands"
    #: Strands runs Bedrock models (e.g. amazon.nova-*, anthropic.claude-*) whose
    #: ids aren't in the default PRICING — augment so cost.record gets cost_usd.
    pricing_table = BEDROCK_PRICING

    def __init__(self, client: Any, capture_config: Optional[CaptureConfig] = None) -> None:
        super().__init__(client, capture_config)
        # Run-scoped state (collector / run span id / timers / model.invoke
        # span ids) lives in the per-run RunState via _begin_run/_end_run
        # (ContextVar-isolated per asyncio.Task / thread), NOT instance
        # scalars — concurrent agent invocations through one adapter instance
        # would otherwise clobber each other's traces (LAY-3576 / D1b).
        self._seen_agents: set = set()  # cross-run config dedup (intentional)
        # Every hook registry this adapter is attached to, with the (event
        # type, callback) pairs it added there, so disconnect() can take them
        # back out however it was attached: Agent(hooks=[adapter]) or
        # connect(target=agent). _draining holds registries disconnect()
        # detached that keep their hooks until the runs in progress on them
        # (_runs_in_flight) end. All three are weakly keyed, so a long-lived
        # adapter neither keeps per-request agents' registries alive nor grows
        # with them.
        self._registrations: weakref.WeakKeyDictionary[Any, List[Tuple[Any, Any]]] = weakref.WeakKeyDictionary()
        self._draining: weakref.WeakKeyDictionary[Any, List[Tuple[Any, Any]]] = weakref.WeakKeyDictionary()
        self._runs_in_flight: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
        self._warned_attach_while_disconnected = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _on_connect(self, target: Any = None, **kwargs: Any) -> Any:
        self._check_dependency(_HAS_STRANDS)
        self._metadata["framework_version"] = _get_version()
        if target is not None:
            self._register_on_agent(target)
        return target

    def _on_disconnect(self) -> None:
        # A run in progress in the caller's own context (disconnect() called
        # from a hook, say) ends and uploads here.
        run = self._get_run()
        self._end_run(flush=True)
        if run is not None:
            self._release_run(run)
        self._detach()
        self._seen_agents.clear()

    # ------------------------------------------------------------------
    # HookProvider protocol
    # ------------------------------------------------------------------

    def register_hooks(self, registry: Any) -> None:
        """Called by Strands when this adapter is passed as ``hooks=[adapter]``."""
        with self._lock:
            # Once per adapter: enough to catch a forgotten connect() without
            # flooding a service that builds agents while tracing is off.
            warn = not self._connected and not self._warned_attach_while_disconnected
            self._warned_attach_while_disconnected |= warn
        if warn:
            log.warning(
                "layerlens: StrandsAdapter was attached to a Strands agent while not connected; "
                "agents are traced only while the adapter is connected (call connect())"
            )
        self._add_callbacks(registry)

    def _register_on_agent(self, agent: Any) -> None:
        """Register hooks on an existing agent's hook registry."""
        hooks = getattr(agent, "hooks", None)
        if hooks is not None and hasattr(hooks, "add_callback"):
            self._add_callbacks(hooks)

    def _add_callbacks(self, registry: Any) -> None:
        callbacks = [
            (event_type, callback)
            for event_type, callback in (
                (_AgentInitializedEvent, self._on_agent_initialized),
                (_BeforeInvocationEvent, self._on_before_invocation),
                (_AfterInvocationEvent, self._on_after_invocation),
                (_BeforeModelCallEvent, self._on_before_model),
                (_AfterModelCallEvent, self._on_after_model),
                (_BeforeToolCallEvent, self._on_before_tool),
                (_AfterToolCallEvent, self._on_after_tool),
            )
            if event_type is not None
        ]
        error = _weak_key_error(registry)
        if error is not None:
            log.warning(
                "layerlens: cannot track Strands hook registry %r (%s); disconnect() won't be able "
                "to remove this adapter's hooks from it (they will stay registered but stop starting traces)",
                registry,
                error,
            )
            for event_type, callback in callbacks:
                registry.add_callback(event_type, callback)
            return
        with self._lock:
            if registry in self._registrations:
                return  # already attached: adding the hooks again would fire each one twice
            pairs = self._draining.pop(registry, None)
            if pairs is None:
                for event_type, callback in callbacks:
                    registry.add_callback(event_type, callback)
                pairs = callbacks
            # else: disconnect() left them in place for a run still in
            # progress, and they are this adapter's attached hooks again.
            self._registrations[registry] = pairs

    def _detach(self) -> None:
        """Take the adapter's hooks back out of every registry it's attached
        to. One with a run still in progress keeps them until that run ends
        (see _release_run), so the run finishes and uploads instead of being
        stranded: unflushed, and still set in its task's context, which would
        stop that task from ever uploading again."""
        with self._lock:
            attached = list(self._registrations.items())
            self._registrations.clear()
            remove_now = []
            for registry, pairs in attached:
                if self._runs_in_flight.get(registry):
                    self._draining[registry] = pairs
                else:
                    remove_now.append((registry, pairs))
        for registry, pairs in remove_now:
            _remove_hooks(registry, pairs)

    def _start_run(self, agent: Any) -> Optional[Any]:
        """Begin the run for one invocation of *agent*, or return None.

        A run starts only while connected, and only on a hook registry the
        adapter is attached to (strands dispatches an agent's events on
        ``agent.hooks``). Otherwise the call comes from a hook disconnect()
        couldn't remove, one still waiting on an earlier run, one attached
        before connect(), or a dispatch that listed the hooks before they were
        removed; _begin_run() creates a collector, so such a run would upload
        a trace the customer turned off, or end up stranded once its
        AfterInvocation hook is gone. Every other handler no-ops without an
        active run. The run is counted under the lock disconnect() decides
        with, so its registry keeps the hooks until it ends.
        """
        registry = getattr(agent, "hooks", None)
        trackable = registry is not None and _weak_key_error(registry) is None
        with self._lock:
            if not self._connected or (trackable and registry not in self._registrations):
                return None
            run = self._begin_run()
            if trackable:
                self._runs_in_flight[registry] = self._runs_in_flight.get(registry, 0) + 1
                run.data[_RUN_REGISTRY] = registry
        return run

    def _release_run(self, run: Any) -> None:
        """Count *run* as ended on its registry. If it was the last run that
        disconnect() was waiting on there, the adapter's hooks leave now."""
        registry = run.data.pop(_RUN_REGISTRY, None)
        if registry is None:
            return
        with self._lock:
            left = self._runs_in_flight.get(registry, 1) - 1
            if left > 0:
                self._runs_in_flight[registry] = left
                return
            self._runs_in_flight.pop(registry, None)
            pairs = self._draining.pop(registry, None)
        if pairs is not None:
            _remove_hooks(registry, pairs)

    # ------------------------------------------------------------------
    # Collector + state management
    # ------------------------------------------------------------------

    def _fire(
        self,
        event_type: str,
        payload: Dict[str, Any],
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        span_name: Optional[str] = None,
    ) -> None:
        run = self._get_run()
        if run is None:
            return
        if event_type == "cost.record" and payload.get("cost_usd") is None:
            self._price_cost_record(payload)
        run.collector.emit(
            event_type,
            payload,
            span_id=span_id or self._new_span_id(),
            parent_span_id=parent_span_id,
            span_name=span_name,
        )

    # ------------------------------------------------------------------
    # Hook handlers
    # ------------------------------------------------------------------

    def _on_agent_initialized(self, event: Any) -> None:
        """Sync-only callback fired when an agent is constructed."""
        try:
            agent = event.agent
            name = _agent_name(agent)
            self._emit_agent_config(name, agent)
        except Exception:
            log.warning("layerlens: error in Strands agent_initialized", exc_info=True)

    def _on_before_invocation(self, event: Any) -> None:
        try:
            agent = event.agent
            name = _agent_name(agent)
            run = self._start_run(agent)
            if run is None:
                return
            span_id = run.root_span_id
            run.data["agent_name"] = name
            self._start_timer("run")

            # Re-emit config if we haven't seen this agent yet
            self._emit_agent_config(name, agent)

            payload = self._payload()
            self._set_honest_agent_name(payload, agent)
            model_id = _model_id(agent)
            if model_id:
                payload["model"] = model_id

            messages = getattr(event, "messages", None)
            self._set_if_capturing(payload, "input", safe_serialize(messages))
            self._fire("agent.input", payload, span_id=span_id, span_name=name)
        except Exception:
            log.warning("layerlens: error in Strands before_invocation", exc_info=True)

    def _on_after_invocation(self, event: Any) -> None:
        run = self._get_run()
        if run is None:
            return
        try:
            agent = event.agent
            name = _agent_name(agent)
            latency_ms = self._stop_timer("run")
            span_id = self._get_root_span()

            payload = self._payload()
            self._set_honest_agent_name(payload, agent)
            if latency_ms is not None:
                payload["duration_ns"] = int(latency_ms * 1_000_000)

            result = getattr(event, "result", None)
            if result is not None:
                stop_reason = getattr(result, "stop_reason", None)
                if stop_reason:
                    payload["stop_reason"] = str(stop_reason)

                message = getattr(result, "message", None)
                self._set_if_capturing(payload, "output", safe_serialize(message))

            # Emit per-cycle cost.record events matched to model spans.
            # accumulated_usage updates AFTER AfterModelCallEvent fires,
            # so we read per-cycle tokens here instead. Isolated in its own
            # guard: a bad cost payload (e.g. an unserializable value that
            # breaks attestation hashing) must degrade to "no cost.record",
            # never abort the run output or the flush below.
            try:
                self._emit_per_cycle_tokens(agent)
            except Exception:
                log.warning("layerlens: error emitting Strands per-cycle tokens", exc_info=True)

            self._fire("agent.output", payload, span_id=span_id, span_name=name)
        except Exception:
            log.warning("layerlens: error in Strands after_invocation", exc_info=True)
        finally:
            # Always flush the trace with whatever was captured — a failure
            # above must never strand the run's events unsent.
            try:
                self._end_run(flush=True)
            except Exception:
                log.warning("layerlens: error flushing Strands trace", exc_info=True)
            try:
                self._release_run(run)
            except Exception:
                log.warning("layerlens: error releasing Strands hooks after a run", exc_info=True)

    def _on_before_model(self, event: Any) -> None:
        try:
            agent = event.agent
            name = _agent_name(agent)
            self._start_timer(f"model:{name}")
        except Exception:
            log.warning("layerlens: error in Strands before_model", exc_info=True)

    def _on_after_model(self, event: Any) -> None:
        """Emit model.invoke with timing and error info.

        Per-call token usage is NOT available here — Strands updates
        accumulated_usage AFTER this hook fires.  Tokens are emitted
        per-cycle from _on_after_invocation using the cycle data.
        """
        run = self._get_run()
        if run is None:
            return
        try:
            agent = event.agent
            name = _agent_name(agent)
            latency_ms = self._stop_timer(f"model:{name}")

            model_id = _model_id(agent)
            payload = self._payload()
            self._set_honest_agent_name(payload, agent)
            if model_id:
                payload["model"] = model_id

            if latency_ms is not None:
                payload["latency_ms"] = latency_ms

            exception = getattr(event, "exception", None)
            if exception is not None:
                payload["error"] = str(exception)
                payload["error_type"] = type(exception).__name__

            stop_response = getattr(event, "stop_response", None)
            if stop_response is not None:
                stop_reason = getattr(stop_response, "stop_reason", None)
                if stop_reason:
                    payload["stop_reason"] = str(stop_reason)

            parent = run.root_span_id
            span_id = self._new_span_id()
            self._fire("model.invoke", payload, span_id=span_id, parent_span_id=parent)
            run.data.setdefault("model_span_ids", []).append(span_id)
        except Exception:
            log.warning("layerlens: error in Strands after_model", exc_info=True)

    def _on_before_tool(self, event: Any) -> None:
        try:
            tool_use = event.tool_use
            tool_name = (
                tool_use.get("name", "unknown") if isinstance(tool_use, dict) else getattr(tool_use, "name", "unknown")
            )
            tool_id = (
                tool_use.get("toolUseId", tool_name)
                if isinstance(tool_use, dict)
                else getattr(tool_use, "toolUseId", tool_name)
            )
            self._start_timer(f"tool:{tool_id}")
        except Exception:
            log.warning("layerlens: error in Strands before_tool", exc_info=True)

    def _on_after_tool(self, event: Any) -> None:
        run = self._get_run()
        if run is None:
            return
        try:
            tool_use = event.tool_use
            tool_name = (
                tool_use.get("name", "unknown") if isinstance(tool_use, dict) else getattr(tool_use, "name", "unknown")
            )
            tool_id = (
                tool_use.get("toolUseId", tool_name)
                if isinstance(tool_use, dict)
                else getattr(tool_use, "toolUseId", tool_name)
            )
            tool_input = tool_use.get("input", None) if isinstance(tool_use, dict) else getattr(tool_use, "input", None)
            latency_ms = self._stop_timer(f"tool:{tool_id}")

            parent = run.root_span_id
            span_id = self._new_span_id()

            call_payload = self._payload(tool_name=tool_name)
            self._set_if_capturing(call_payload, "input", safe_serialize(tool_input))
            if latency_ms is not None:
                call_payload["latency_ms"] = latency_ms
            self._fire(
                "tool.call",
                call_payload,
                span_id=span_id,
                parent_span_id=parent,
                span_name=f"tool:{tool_name}",
            )

            result = getattr(event, "result", None)
            result_payload = self._payload(tool_name=tool_name)
            if result is not None:
                status = result.get("status", None) if isinstance(result, dict) else getattr(result, "status", None)
                if status:
                    result_payload["status"] = str(status)
                content = result.get("content", None) if isinstance(result, dict) else getattr(result, "content", None)
                self._set_if_capturing(result_payload, "output", safe_serialize(content))

            exception = getattr(event, "exception", None)
            if exception is not None:
                result_payload["error"] = str(exception)
                result_payload["error_type"] = type(exception).__name__

            self._fire(
                "tool.result",
                result_payload,
                span_id=span_id,
                parent_span_id=parent,
                span_name=f"tool:{tool_name}",
            )

            # A strands swarm handoff is the developer calling the built-in
            # ``handoff_to_agent(agent_name, message)`` tool — a producer-declared
            # transition to another named agent. Surface it as an honest
            # ``agent.handoff`` graph edge (only when BOTH endpoints resolve to a
            # real, non-generic name — never fabricate a node).
            if tool_name == _HANDOFF_TOOL:
                self._emit_handoff(getattr(event, "agent", None), tool_input, parent)
        except Exception:
            log.warning("layerlens: error in Strands after_tool", exc_info=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_honest_agent_name(self, payload: Dict[str, Any], agent: Any) -> None:
        """Stamp ``payload['agent_name']`` ONLY when the developer set a real,
        distinctive agent name — never the generic framework default
        ``"Strands Agents"`` (or a class-name/API-method fallback). When no
        honest name exists the payload stays blank (an honest ``—`` node)."""
        # ateam parity (#3): prefer the honest, distinctive name; fall back to the
        # raw framework/class name VERBATIM (sanitized) so an unnamed agent still
        # renders its (generic) identity like ateam, instead of a blank column.
        # Handoff endpoints (_emit_handoff) stay on the honest helper.
        name = _honest_agent_name(agent) or _s(_agent_name(agent))
        if name:
            payload["agent_name"] = name

    def _emit_handoff(self, agent: Any, tool_input: Any, parent_span_id: Optional[str]) -> None:
        """Emit ``agent.handoff{from_agent,to_agent}`` for a swarm handoff.

        Both endpoints must be honest, producer-declared names: ``from_agent``
        is the current agent's declared name, ``to_agent`` is the target passed
        to ``handoff_to_agent(agent_name=...)``. If either is missing/generic we
        emit nothing rather than fabricate a graph node/edge."""
        from_agent = _honest_agent_name(agent)
        raw_to = tool_input.get("agent_name") if isinstance(tool_input, dict) else None
        to_agent = _honest_name(raw_to)
        if not from_agent or not to_agent:
            return
        self._fire(
            "agent.handoff",
            self._payload(from_agent=from_agent, to_agent=to_agent),
            parent_span_id=parent_span_id,
            span_name=f"handoff:{from_agent}->{to_agent}",
        )

    def _emit_agent_config(self, name: str, agent: Any) -> None:
        with self._lock:
            if name in self._seen_agents:
                return
            self._seen_agents.add(name)

        payload = self._payload(agent_type=type(agent).__name__)
        self._set_honest_agent_name(payload, agent)

        mid = _model_id(agent)
        if mid:
            payload["model"] = mid

        system_prompt = getattr(agent, "system_prompt", None)
        if system_prompt and self._config.capture_content:
            payload["system_prompt"] = str(system_prompt)[:500]

        tool_names = getattr(agent, "tool_names", None)
        if tool_names:
            payload["tools"] = list(tool_names)

        self._fire(
            "environment.config",
            payload,
            parent_span_id=self._get_root_span(),
            span_name=f"config:{name}",
        )

    def _emit_per_cycle_tokens(self, agent: Any) -> None:
        """Emit cost.record per model call using per-cycle token data.

        Strands stores per-cycle usage at:
            agent.event_loop_metrics.agent_invocations[-1].cycles[i].usage
        Each cycle maps 1:1 with a model call, so we zip cycles with
        the per-run ``model_span_ids`` to attribute tokens correctly.
        """
        run = self._get_run()
        if run is None:
            return
        model_id = _model_id(agent)
        span_ids = list(run.data.get("model_span_ids", []))

        cycles = _get_cycles(agent)
        if not cycles and not span_ids:
            return

        # Zip cycles with model span_ids — if counts differ, emit what we can
        for i, cycle in enumerate(cycles):
            usage = getattr(cycle, "usage", None) if not isinstance(cycle, dict) else cycle.get("usage")
            if usage is None:
                continue
            if isinstance(usage, dict):
                input_t = usage.get("inputTokens", 0)
                output_t = usage.get("outputTokens", 0)
            else:
                input_t = getattr(usage, "inputTokens", 0) or 0
                output_t = getattr(usage, "outputTokens", 0) or 0

            if not input_t and not output_t:
                continue

            tokens: Dict[str, int] = {}
            if input_t:
                tokens["tokens_prompt"] = input_t
            if output_t:
                tokens["tokens_completion"] = output_t
            tokens["tokens_total"] = input_t + output_t

            # Per-cycle timing — Strands stores start/end on each cycle;
            # surface the duration so we can chart tokens/sec per cycle.
            cycle_latency_ms = _cycle_latency_ms(cycle)
            if cycle_latency_ms is not None:
                tokens["cycle_latency_ms"] = int(cycle_latency_ms)
            # Per-cycle stop reason (set when the cycle exits due to e.g.
            # tool_use, end_turn, max_tokens, etc.).
            stop_reason = _cycle_stop_reason(cycle)
            if stop_reason:
                tokens["stop_reason"] = stop_reason

            cost_payload = self._payload(**tokens)
            cost_payload["cycle_index"] = i
            if model_id:
                cost_payload["model"] = model_id

            parent = span_ids[i] if i < len(span_ids) else run.root_span_id
            self._fire("cost.record", cost_payload, parent_span_id=parent)


# -- Module-level helpers --------------------------------------------------

#: ``RunState.data`` key: the hook registry a run is counted against.
_RUN_REGISTRY = "hooks_registry"


def _weak_key_error(obj: Any) -> Optional[TypeError]:
    """Why *obj* can't key a ``WeakKeyDictionary`` (it can't be weakly
    referenced, or it's unhashable), or None when it can."""
    try:
        hash(weakref.ref(obj))
    except TypeError as exc:
        return exc
    return None


def _remove_hooks(registry: Any, pairs: List[Tuple[Any, Any]]) -> None:
    leaked = [pair for pair in pairs if not _remove_callback(registry, *pair)]
    if leaked:
        log.warning(
            "layerlens: disconnect() could not find %d of this adapter's Strands hook callbacks "
            "in %r to remove them (strands may have changed how HookRegistry stores callbacks); "
            "they stay registered but no longer start traces",
            len(leaked),
            registry,
        )


def _remove_callback(registry: Any, event_type: Any, callback: Any) -> bool:
    """Remove one callback this adapter added to a strands ``HookRegistry``.

    strands has no public removal API, so this edits the registry's private
    ``_registered_callbacks`` (event type -> list). strands <= 1.42.0 keeps
    the bare callback in that list; 1.43.0 and later wrap it in
    ``_CallbackEntry(callback=..., order=...)``. Either is matched by identity.
    Returns False when the layout isn't recognized or the callback isn't
    there, so the caller reports it instead of leaking silently.
    """
    by_type = getattr(registry, "_registered_callbacks", None)
    if not isinstance(by_type, dict):
        return False
    entries = by_type.get(event_type)
    if not isinstance(entries, list):
        return False
    for i, entry in enumerate(entries):
        if entry is callback or getattr(entry, "callback", None) is callback:
            # Swap in a new list instead of deleting in place: an invocation
            # mid-dispatch (the async dispatcher awaits between callbacks)
            # keeps iterating the old list and can't skip a hook.
            by_type[event_type] = entries[:i] + entries[i + 1 :]
            return True
    return False


def _get_cycles(agent: Any) -> list:
    """Extract per-cycle data from the most recent invocation.

    Path: agent.event_loop_metrics.agent_invocations[-1].cycles
    """
    try:
        metrics = getattr(agent, "event_loop_metrics", None)
        if metrics is None:
            return []
        invocations = getattr(metrics, "agent_invocations", None)
        if not invocations:
            return []
        last = invocations[-1]
        cycles = getattr(last, "cycles", None)
        return list(cycles) if cycles else []
    except Exception:
        return []


def _agent_name(agent: Any) -> str:
    """A non-empty LABEL for internal use only (span_name / timer keys /
    config dedup) — may be a generic default. NEVER surfaced as the honest
    ``agent_name`` payload identity; use :func:`_honest_agent_name` for that."""
    if agent is None:
        return "unknown"
    return getattr(agent, "name", None) or type(agent).__name__


def _honest_name(raw: Any) -> Optional[str]:
    """A producer-declared, distinctive agent name — or None.

    Rejects the generic framework default ``"Strands Agents"`` and any other
    class-name/placeholder on the shared identity denylist, plus dotted
    API-method labels. Reuses the single honest-identity guard so the adapter
    never fabricates a graph node."""
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or _is_generic(name) or _API_METHOD_RE.match(name.lower()):
        return None
    return name


def _honest_agent_name(agent: Any) -> Optional[str]:
    """The developer-declared name of *agent*, honest-guarded — or None."""
    if agent is None:
        return None
    return _honest_name(getattr(agent, "name", None))


def _model_id(agent: Any) -> Optional[str]:
    if agent is None:
        return None
    model = getattr(agent, "model", None)
    if model is None:
        return None
    config = getattr(model, "config", None)
    if isinstance(config, dict):
        mid = config.get("model_id")
        if mid:
            return str(mid)
    return str(model) if model else None


def _get_version() -> str:
    try:
        import strands as _mod  # pyright: ignore[reportMissingImports]

        return getattr(_mod, "__version__", "unknown")
    except Exception:
        return "unknown"


def _cycle_latency_ms(cycle: Any) -> Optional[float]:
    if isinstance(cycle, dict):
        start = cycle.get("start_time") or cycle.get("startTime")
        end = cycle.get("end_time") or cycle.get("endTime")
    else:
        start = getattr(cycle, "start_time", None) or getattr(cycle, "startTime", None)
        end = getattr(cycle, "end_time", None) or getattr(cycle, "endTime", None)
    if start is None or end is None:
        return None
    try:
        return (end - start) * 1000 if isinstance(start, (int, float)) else None
    except Exception:
        return None


def _cycle_stop_reason(cycle: Any) -> Optional[str]:
    if isinstance(cycle, dict):
        return cycle.get("stop_reason") or cycle.get("stopReason")
    return getattr(cycle, "stop_reason", None) or getattr(cycle, "stopReason", None)
