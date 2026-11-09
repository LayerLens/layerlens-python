from __future__ import annotations

import time
import logging
from typing import Any, Dict, List, Iterator, Optional
from contextlib import contextmanager
from contextvars import ContextVar

from ._utils import safe_serialize
from ..._context import RunState, _current_run, _current_collector
from ..._identity import _API_METHOD_RE, _is_generic
from ..._error_code import error_category
from ._base_framework import FrameworkAdapter
from ..._capture_config import CaptureConfig

log = logging.getLogger(__name__)

_HAS_HAYSTACK = False
try:
    from haystack import tracing as _hs_tracing  # pyright: ignore[reportMissingImports]

    _HAS_HAYSTACK = True
except ImportError:
    _hs_tracing = None  # type: ignore[assignment]

_GENERATOR_KEYWORDS = ("generator", "chatgenerator", "llm")

# Operation names of the spans haystack's tracing opens.
_PIPELINE_RUN = "haystack.pipeline.run"
_COMPONENT_RUN = "haystack.component.run"
# One ``Agent.run()`` / ``run_async()`` (haystack-ai 2.x and 3.x).
_AGENT_RUN = "haystack.agent.run"
# haystack-ai 3.x only: one Agent step, the step's chat-generator call, and each
# tool call of the step. haystack-ai 2.x runs the Agent's chat generator and its
# ToolInvoker as ``haystack.component.run`` spans instead.
_AGENT_STEP = "haystack.agent.step"
_AGENT_STEP_LLM = "haystack.agent.step.llm"
_AGENT_STEP_TOOL = "haystack.agent.step.tool"
# haystack-ai 3.x only: the chat-generator call a component makes inside its own
# ``run()`` through the shared generator utils (routers, rankers, extractors,
# evaluators, query expanders, FallbackChatGenerator's attempts).
_CHAT_GENERATOR_RUN = "haystack.chat_generator.run"

#: Spans that always emit events under their own span id. The events of a span
#: name as parent the nearest enclosing span of this set: an LLM-call span
#: (``haystack.agent.step.llm``, ``haystack.chat_generator.run``) emits only when
#: it is the span that reports the call (see "One model.invoke per provider call"
#: below), so nothing may hang off it, or a parent no event carries would split
#: the span tree the server builds from ``parent_span_id``.
_ALWAYS_EMITTING = frozenset({_PIPELINE_RUN, _COMPONENT_RUN, _AGENT_RUN, _AGENT_STEP, _AGENT_STEP_TOOL})

# One model.invoke per provider call
# ----------------------------------
# One provider call can surface on several nested spans: a pipeline generator
# component, the chat-generator span a component opens around its own call, an
# Agent's LLM step. It is reported (model.invoke, plus a cost.record when it has
# token counts) by the innermost span that carries its reply, i.e. the replies
# with the response metadata (model, usage, finish reason); the spans above it
# report no model call of their own:
#
# * a generator-named component whose calls were reported beneath it (an
#   ``LLMMessagesRouter``, ``LLMRanker``, ``LLMMetadataExtractor``, ...) is
#   reported as the component run it is (tool.call / tool.result);
# * a chat-generator span without a reply leaves the call to the span above it,
#   which carries the reply: ``FallbackChatGenerator`` sets no output on its
#   inner spans because its own component span, or the Agent's LLM step that
#   called it, returns the replies;
# * a call that failed before it replied is reported once, with its error, by the
#   innermost span that saw the failure; a span above it that has no reply of its
#   own adds no second model.invoke for it.


class HaystackAdapter(FrameworkAdapter):
    """Haystack (haystack-ai 2.x and 3.x) adapter via global tracer replacement.

    Replaces ``haystack.tracing.tracer.actual_tracer`` with a thin
    ``_LayerLensTracer`` that delegates all event emission back to
    this adapter.  Each ``Pipeline.run()``, and each ``Agent.run()`` made
    outside a pipeline, gets its own collector via ``_begin_run`` / ``_end_run``.

    Usage::

        adapter = HaystackAdapter(client)
        adapter.connect()
        result = pipeline.run(...)
        adapter.disconnect()
    """

    name = "haystack"

    def __init__(self, client: Any, capture_config: Optional[CaptureConfig] = None) -> None:
        super().__init__(client, capture_config)
        self._original_tracer: Any = None
        self._tracer: Optional[_LayerLensTracer] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _on_connect(self, target: Any = None, **kwargs: Any) -> None:
        self._check_dependency(_HAS_HAYSTACK)
        self._metadata["framework_version"] = _get_version()
        self._original_tracer = _hs_tracing.tracer.actual_tracer
        self._tracer = _LayerLensTracer(self)
        _hs_tracing.tracer.actual_tracer = self._tracer

    def _on_disconnect(self) -> None:
        if _HAS_HAYSTACK and self._original_tracer is not None:
            try:
                _hs_tracing.tracer.actual_tracer = self._original_tracer
            except Exception:
                log.debug("layerlens: failed to restore Haystack tracer", exc_info=True)
            self._original_tracer = None
        self._tracer = None

    # ------------------------------------------------------------------
    # Span handlers (called by _LayerLensSpan._finish)
    # ------------------------------------------------------------------

    def _on_span_end(self, span: _LayerLensSpan) -> None:
        elapsed_ms = (time.time_ns() - span._start_ns) / 1_000_000
        operation = span._operation_name
        try:
            with self._bound_to(span._run):
                if span._is_pipeline:
                    self._on_pipeline_end(span, elapsed_ms)
                elif operation == _COMPONENT_RUN:
                    self._on_component_end(span, elapsed_ms)
                elif operation == _AGENT_RUN:
                    self._on_agent_end(span, elapsed_ms)
                elif operation == _AGENT_STEP:
                    self._on_agent_step_end(span, elapsed_ms)
                elif operation == _AGENT_STEP_LLM:
                    self._on_agent_llm_end(span, elapsed_ms)
                elif operation == _AGENT_STEP_TOOL:
                    self._on_agent_tool_end(span, elapsed_ms)
                elif operation == _CHAT_GENERATOR_RUN:
                    self._on_chat_generator_end(span, elapsed_ms)
        except Exception:
            log.warning("layerlens: error emitting Haystack span", exc_info=True)
        finally:
            # The span that opened the run closes it even when emitting failed:
            # otherwise the run stays bound to the caller's context and its trace
            # is never uploaded.
            if span._owns_run:
                self._end_run()

    @contextmanager
    def _bound_to(self, run: Optional[RunState]) -> Iterator[None]:
        """Emit into the run the span belongs to.

        ``LLMMetadataExtractor`` and ``LLMDocumentContentExtractor`` call their
        chat generator on a thread pool that does not copy the caller's context,
        so the span of each call ends where no run (and no collector) is bound,
        and its events would go nowhere. Each span carries the run of the span it
        was opened under; bind it for the duration of the emit.
        """
        if run is None or _current_run.get() is run:
            yield
            return
        run_token = _current_run.set(run)
        collector_token = _current_collector.set(run.collector)
        try:
            yield
        finally:
            _current_collector.reset(collector_token)
            _current_run.reset(run_token)

    def _on_pipeline_end(self, span: _LayerLensSpan, elapsed_ms: float) -> None:
        tags = span._all_tags()
        root = self._get_root_span()

        inp = self._payload()
        self._set_if_capturing(inp, "input", safe_serialize(tags.get("haystack.pipeline.input_data")))
        max_runs = tags.get("haystack.pipeline.max_runs_per_component")
        if max_runs is not None:
            inp["max_runs_per_component"] = max_runs
        self._emit(
            "agent.input",
            inp,
            span_id=root,
            parent_span_id=None,
            span_name="haystack:pipeline",
        )

        # Re-emit the pipeline shape so the server graph engine can render each
        # named component as a node. Only the honest, producer-declared
        # component names observed during this run are listed — an unnamed /
        # generic component is never fabricated into a node.
        components = self._collected_components()
        if components:
            self._emit(
                "environment.config",
                self._payload(components=components),
                span_id=root,
                parent_span_id=None,
                span_name="haystack:pipeline",
            )

        out = self._payload(latency_ms=elapsed_ms)
        self._set_if_capturing(out, "output", safe_serialize(tags.get("haystack.pipeline.output_data")))
        _stamp_error(out, tags)
        self._emit(
            "agent.output",
            out,
            span_id=root,
            parent_span_id=None,
            span_name="haystack:pipeline",
        )

    def _on_component_end(self, span: _LayerLensSpan, elapsed_ms: float) -> None:
        tags = span._all_tags()
        comp_type = str(tags.get("haystack.component.type", ""))
        comp_name = str(tags.get("haystack.component.name", "unknown"))

        # ``component_name`` is the field the server graph engine reads to build
        # a haystack node. The instance name a developer gave the component
        # (``retriever``, ``llm``) is producer-declared and honest; a missing
        # name (the ``"unknown"`` default) or any generic/API-method label is
        # NOT — it must stay blank rather than fabricate a node identity.
        honest_name = _honest_component_name(comp_name)
        if honest_name is not None:
            self._record_component(honest_name, comp_type)

        is_generator = any(kw in comp_type.lower() for kw in _GENERATOR_KEYWORDS)
        if is_generator and not _calls_reported_below(span, tags.get("haystack.component.output")):
            self._on_generator_end(span, elapsed_ms, tags, comp_name, comp_type, honest_name)
        else:
            # A generator-named component whose LLM calls the spans beneath it
            # reported (see "One model.invoke per provider call") runs like any
            # other component.
            self._on_tool_end(span, elapsed_ms, tags, comp_name, comp_type, honest_name)

    def _record_component(self, name: str, comp_type: str) -> None:
        """Accumulate a distinct honest component for this run's ``environment.config``."""
        run = self._get_run()
        if run is None:
            return
        seen = run.data.setdefault("haystack_components", [])
        if not any(c["name"] == name for c in seen):
            entry: Dict[str, Any] = {"name": name}
            if comp_type:
                entry["component_class"] = comp_type
            seen.append(entry)

    def _collected_components(self) -> list:
        run = self._get_run()
        if run is None:
            return []
        return list(run.data.get("haystack_components", []))

    @staticmethod
    def _stamp_component(payload: Dict[str, Any], honest_name: Optional[str], comp_type: str) -> None:
        """Stamp the honest graph-node identity on a component event.

        ``component_name`` is what the server graph engine reads for haystack, so
        it is set ONLY when the developer declared a real component name (never a
        generic/API-method label). ``component_class`` is the component's class
        (``OpenAIChatGenerator``) — carried as metadata, never as the node id.
        """
        if honest_name is not None:
            payload["component_name"] = honest_name
        if comp_type:
            payload["component_class"] = comp_type

    def _on_generator_end(
        self,
        span: _LayerLensSpan,
        elapsed_ms: float,
        tags: Dict[str, Any],
        name: str,
        comp_type: str,
        honest_name: Optional[str] = None,
    ) -> None:
        model = _extract_model(tags)
        output = tags.get("haystack.component.output", {})
        tokens = self._normalize_tokens(_extract_usage(output))

        payload = self._payload(component_type=comp_type, latency_ms=elapsed_ms)
        self._stamp_component(payload, honest_name, comp_type)
        if model:
            payload["model"] = model
        fr = _extract_finish_reason(output)
        if fr:
            payload["finish_reason"] = str(fr)
        payload.update(tokens)
        self._set_if_capturing(payload, "input", safe_serialize(tags.get("haystack.component.input")))
        if isinstance(output, dict) and "replies" in output:
            self._set_if_capturing(payload, "output", safe_serialize(output["replies"]))
        self._emit(
            "model.invoke",
            payload,
            span_id=span.span_id,
            parent_span_id=_event_parent(span),
            span_name=f"component:{name}",
        )

        if tokens:
            cost = self._payload(**tokens)
            if model:
                cost["model"] = model
            self._emit("cost.record", cost, parent_span_id=span.span_id)
        _mark_reported(span, replied=_generator_meta(output) is not None)

    def _on_tool_end(
        self,
        span: _LayerLensSpan,
        elapsed_ms: float,
        tags: Dict[str, Any],
        name: str,
        comp_type: str,
        honest_name: Optional[str] = None,
    ) -> None:
        call = self._payload(tool_name=name, component_type=comp_type)
        self._stamp_component(call, honest_name, comp_type)
        self._set_if_capturing(call, "input", safe_serialize(tags.get("haystack.component.input")))
        self._emit(
            "tool.call",
            call,
            span_id=span.span_id,
            parent_span_id=_event_parent(span),
            span_name=f"component:{name}",
        )

        result = self._payload(tool_name=name, component_type=comp_type, latency_ms=elapsed_ms)
        self._stamp_component(result, honest_name, comp_type)
        self._set_if_capturing(result, "output", safe_serialize(tags.get("haystack.component.output")))
        _stamp_error(result, tags)
        self._emit(
            "tool.result",
            result,
            span_id=span.span_id,
            parent_span_id=_event_parent(span),
            span_name=f"component:{name}",
        )

    # ------------------------------------------------------------------
    # Agent spans (haystack.agent.*) and the shared chat-generator span
    # ------------------------------------------------------------------

    def _on_agent_end(self, span: _LayerLensSpan, elapsed_ms: float) -> None:
        """Frame one ``Agent.run()`` with agent.input / environment.config / agent.output.

        An Agent that runs on its own opened the run, so the three sit on the
        run's root span, like a pipeline's. An Agent inside a pipeline puts them
        on its own span, under the span of the pipeline component that ran it.
        """
        tags = span._all_tags()
        parent_span_id = None if span._owns_run else _event_parent(span)
        name = _agent_name(span)

        inp = self._payload()
        _stamp_agent(inp, name)
        self._set_if_capturing(inp, "input", safe_serialize(tags.get("haystack.agent.input")))
        self._emit(
            "agent.input",
            inp,
            span_id=span.span_id,
            parent_span_id=parent_span_id,
            span_name="haystack:agent",
        )

        config = self._agent_config(tags, name, with_components=span._owns_run)
        if config is not None:
            self._emit(
                "environment.config",
                config,
                span_id=span.span_id,
                parent_span_id=parent_span_id,
                span_name="haystack:agent",
            )

        out = self._payload(latency_ms=elapsed_ms)
        _stamp_agent(out, name)
        steps = tags.get("haystack.agent.steps_taken")
        if isinstance(steps, int):
            out["steps_taken"] = steps
        self._set_if_capturing(out, "output", safe_serialize(tags.get("haystack.agent.output")))
        _stamp_error(out, tags)
        self._emit(
            "agent.output",
            out,
            span_id=span.span_id,
            parent_span_id=parent_span_id,
            span_name="haystack:agent",
        )

    def _agent_config(
        self, tags: Dict[str, Any], name: Optional[str], *, with_components: bool
    ) -> Optional[Dict[str, Any]]:
        """The Agent's configuration as its span declares it: the names of the
        tools it was given, its step limit and its exit conditions. An Agent that
        opened the run also lists the honest components that ran in it, as a
        pipeline does (haystack-ai 2.x runs the Agent's generator and ToolInvoker
        as components)."""
        payload = self._payload()
        _stamp_agent(payload, name)
        tools = _tool_names(tags.get("haystack.agent.tools"))
        if tools:
            payload["tools"] = tools
        max_steps = tags.get("haystack.agent.max_steps")
        if isinstance(max_steps, int) and not isinstance(max_steps, bool):
            payload["max_steps"] = max_steps
        exit_conditions = tags.get("haystack.agent.exit_conditions")
        if isinstance(exit_conditions, (list, tuple)):
            payload["exit_conditions"] = [str(c) for c in exit_conditions]
        if with_components:
            components = self._collected_components()
            if components:
                payload["components"] = components
        if not set(payload) - {"framework", "agent_name"}:
            return None
        return payload

    def _on_agent_step_end(self, span: _LayerLensSpan, elapsed_ms: float) -> None:
        tags = span._all_tags()
        step = tags.get("haystack.agent.step")
        payload = self._payload(latency_ms=elapsed_ms)
        if isinstance(step, int):
            payload["step"] = step
        _stamp_agent(payload, _enclosing_agent_name(span))
        _stamp_error(payload, tags)
        self._emit(
            "agent.step",
            payload,
            span_id=span.span_id,
            parent_span_id=_event_parent(span),
            span_name=f"agent.step:{step}" if isinstance(step, int) else "agent.step",
        )

    def _on_agent_llm_end(self, span: _LayerLensSpan, elapsed_ms: float) -> None:
        """The chat-generator call of one Agent step (haystack-ai 3.x)."""
        if span._reply_reported_below:
            return
        tags = span._all_tags()
        output = tags.get("haystack.agent.step.llm.output")
        if _generator_meta(output) is None and span._call_reported_below:
            return
        self._report_model_call(
            span,
            elapsed_ms,
            tags,
            output=output,
            model_input=tags.get("haystack.agent.step.llm.input"),
            span_name="agent.llm",
        )

    def _on_chat_generator_end(self, span: _LayerLensSpan, elapsed_ms: float) -> None:
        """A chat-generator call a component made inside its own ``run()`` (haystack-ai 3.x)."""
        if span._reply_reported_below:
            return
        tags = span._all_tags()
        output = tags.get("haystack.component.output")
        if _generator_meta(output) is None and (not tags.get("error") or span._call_reported_below):
            # No reply here: the component that made the call returns it, and its
            # span (or the Agent step that called it) reports the call.
            return
        comp_type = str(tags.get("haystack.component.type", ""))
        self._report_model_call(
            span,
            elapsed_ms,
            tags,
            output=output,
            model_input=tags.get("haystack.component.input"),
            span_name=f"chat_generator:{comp_type}" if comp_type else "chat_generator",
            comp_type=comp_type,
            # The span's own name is haystack's "chat_generator" literal, never
            # one a developer gave; the call belongs to the component that made it.
            component_name=_enclosing_component_name(span),
        )

    def _report_model_call(
        self,
        span: _LayerLensSpan,
        elapsed_ms: float,
        tags: Dict[str, Any],
        *,
        output: Any,
        model_input: Any,
        span_name: str,
        comp_type: str = "",
        component_name: Optional[str] = None,
    ) -> None:
        """model.invoke for one provider call, plus its priced cost.record when the
        reply reports token counts. Model, usage and finish reason come only from
        the reply's response metadata; a failed call carries its error instead."""
        meta = _generator_meta(output)
        model = meta.get("model") if meta else None
        tokens = self._normalize_tokens(meta.get("usage") if meta else None)
        agent_name = _enclosing_agent_name(span)

        payload = self._payload(latency_ms=elapsed_ms)
        if comp_type:
            payload["component_type"] = comp_type
        self._stamp_component(payload, component_name, comp_type)
        _stamp_agent(payload, agent_name)
        if model:
            payload["model"] = str(model)
        finish_reason = meta.get("finish_reason") if meta else None
        if finish_reason:
            payload["finish_reason"] = str(finish_reason)
        payload.update(tokens)
        self._set_if_capturing(payload, "input", safe_serialize(model_input))
        if isinstance(output, dict) and "replies" in output:
            self._set_if_capturing(payload, "output", safe_serialize(output["replies"]))
        _stamp_error(payload, tags)
        self._emit(
            "model.invoke",
            payload,
            span_id=span.span_id,
            parent_span_id=_event_parent(span),
            span_name=span_name,
        )

        if tokens:
            cost = self._payload(**tokens)
            if model:
                cost["model"] = str(model)
            if component_name is not None:
                cost["component_name"] = component_name
            _stamp_agent(cost, agent_name)
            self._emit("cost.record", cost, parent_span_id=span.span_id)
        _mark_reported(span, replied=meta is not None)

    def _on_agent_tool_end(self, span: _LayerLensSpan, elapsed_ms: float) -> None:
        """One tool call of an Agent step (haystack-ai 3.x), under the tool's own name."""
        tags = span._all_tags()
        tool_name = tags.get("haystack.tool.name")
        agent_name = _enclosing_agent_name(span)
        parent_span_id = _event_parent(span)
        span_name = f"agent.tool:{tool_name}" if tool_name else "agent.tool"

        call = self._payload()
        if tool_name:
            call["tool_name"] = str(tool_name)
        _stamp_agent(call, agent_name)
        self._set_if_capturing(call, "input", safe_serialize(tags.get("haystack.agent.step.tool.input")))
        self._emit("tool.call", call, span_id=span.span_id, parent_span_id=parent_span_id, span_name=span_name)

        result = self._payload(latency_ms=elapsed_ms)
        if tool_name:
            result["tool_name"] = str(tool_name)
        _stamp_agent(result, agent_name)
        output = tags.get("haystack.agent.step.tool.output")
        self._set_if_capturing(result, "output", safe_serialize(output))
        if tags.get("haystack.tool.error"):
            # haystack catches a failed tool call inside the span, flags it with
            # ``haystack.tool.error`` and records ``{"error": str(exc)}`` as the
            # output; the exception itself never reaches the tracer.
            message = output.get("error") if isinstance(output, dict) else None
            result["error"] = str(message) if message is not None else str(tags.get("error.message", "unknown"))
            result["error_type"] = "tool_error"
        else:
            _stamp_error(result, tags)
        self._emit("tool.result", result, span_id=span.span_id, parent_span_id=parent_span_id, span_name=span_name)


# ---------------------------------------------------------------------------
# Thin protocol implementations (Tracer + Span)
# ---------------------------------------------------------------------------


class _LayerLensTracer:
    """Minimal Haystack ``Tracer`` — tracks the current span per context and
    delegates all event logic to the adapter."""

    def __init__(self, adapter: HaystackAdapter) -> None:
        self._adapter = adapter
        # The current span is per context, as the run it belongs to is (RunState
        # rides a ContextVar too): concurrent ``Agent.run_async()`` calls share one
        # event-loop thread, so a thread-local current span let the second Agent
        # open its span under the first one's, join that run, and lose its events
        # when the first run flushed.
        self._current: ContextVar[Optional[_LayerLensSpan]] = ContextVar(
            f"layerlens_haystack_span_{id(self)}", default=None
        )

    @contextmanager
    def trace(
        self,
        operation_name: str,
        tags: Optional[Dict[str, Any]] = None,
        parent_span: Optional[Any] = None,
    ) -> Iterator[_LayerLensSpan]:
        if parent_span is None:
            parent_span = self._current.get()
        # haystack passes ``tracer.current_span()`` as the parent, which is a
        # ``_NullSpan`` outside any span.
        parent = parent_span if isinstance(parent_span, _LayerLensSpan) else None

        # A span belongs to the run of the span it was opened under; without one,
        # to the run bound to the context. A pipeline always opens its own run, and
        # so does an Agent that runs outside any run (``Agent.run()`` on its own).
        run = parent._run if parent is not None else self._adapter._get_run()
        owns_run = operation_name == _PIPELINE_RUN or (operation_name == _AGENT_RUN and run is None)
        if owns_run:
            run = self._adapter._begin_run()
            span_id = run.root_span_id
        else:
            span_id = self._adapter._new_span_id()

        span = _LayerLensSpan(
            self._adapter,
            operation_name,
            span_id,
            parent,
            tags or {},
            run=run,
            owns_run=owns_run,
        )

        prev = self._current.get()
        token = self._current.set(span)
        try:
            yield span
        except Exception as exc:
            span.set_tag("error", True)
            span.set_tag("error.message", str(exc))
            category = error_category(exc)
            span.set_tag("error.type", category["error_type"])
            if "error_code" in category:
                span.set_tag("error.code", category["error_code"])
            raise
        finally:
            span._finish()
            try:
                self._current.reset(token)
            except ValueError:
                # Exited in another Context than it was entered in (a framework
                # copied the context in between): restore the value instead.
                self._current.set(prev)

    def current_span(self) -> Any:
        return self._current.get() or _NullSpan()


class _NullSpan:
    """No-op span returned outside an active trace."""

    def set_tag(self, key: str, value: Any) -> None:
        pass

    def set_tags(self, tags: Dict[str, Any]) -> None:
        pass

    def set_content_tag(self, key: str, value: Any) -> None:
        pass

    def raw_span(self) -> None:
        return None

    def get_correlation_data_for_logs(self) -> Dict[str, Any]:
        return {}


class _LayerLensSpan:
    """Tag accumulator implementing the Haystack ``Span`` protocol.
    Delegates to ``adapter._on_span_end`` on finish."""

    def __init__(
        self,
        adapter: HaystackAdapter,
        operation_name: str,
        span_id: str,
        parent: Optional[_LayerLensSpan],
        tags: Dict[str, Any],
        *,
        run: Optional[RunState],
        owns_run: bool,
    ) -> None:
        self._adapter = adapter
        self._operation_name = operation_name
        self.span_id = span_id
        self._parent = parent
        self._parent_span_id = parent.span_id if parent is not None else None
        self._tags: Dict[str, Any] = dict(tags)
        self._content_tags: Dict[str, Any] = {}
        self._start_ns = time.time_ns()
        self._is_pipeline = operation_name == _PIPELINE_RUN
        #: The run whose collector this span's events go to.
        self._run = run
        #: This span opened ``_run`` (a pipeline, or an Agent run on its own) and
        #: ends it.
        self._owns_run = owns_run
        # Set by the spans nested in this one when they report a model call: any
        # call, and a call with its reply (see "One model.invoke per provider call").
        self._call_reported_below = False
        self._reply_reported_below = False

    def set_tag(self, key: str, value: Any) -> None:
        self._tags[key] = value

    def set_tags(self, tags: Dict[str, Any]) -> None:
        """haystack's ``Span.set_tags``: haystack 3's ``CompactionHook`` tags the
        current span with it before every LLM step of an Agent."""
        for key, value in tags.items():
            self.set_tag(key, value)

    def set_content_tag(self, key: str, value: Any) -> None:
        self._content_tags[key] = value

    def raw_span(self) -> None:
        return None

    def get_correlation_data_for_logs(self) -> Dict[str, Any]:
        return {"span_id": self.span_id, "operation_name": self._operation_name}

    def _all_tags(self) -> Dict[str, Any]:
        return {**self._tags, **self._content_tags}

    def _finish(self) -> None:
        self._adapter._on_span_end(self)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _honest_component_name(name: Any) -> Optional[str]:
    """The producer-declared component name, honest-guarded — or None.

    Returns the trimmed component name a developer gave the pipeline component,
    unless it is empty, the ``"unknown"`` default (an unset name), any generic
    class/placeholder label, or a dotted API-method label — none of which is a
    real, producer-declared node identity. Reuses the shared anti-fabrication
    guard so the haystack graph node is never invented.
    """
    if not isinstance(name, str):
        return None
    n = name.strip()
    if not n or _is_generic(n) or _API_METHOD_RE.match(n.lower()):
        return None
    return n


def _event_parent(span: _LayerLensSpan) -> Optional[str]:
    """The span id a span's events name as parent: the nearest enclosing span
    that always emits (``_ALWAYS_EMITTING``), so the parent is always a span the
    trace carries. None at the top of a run (the run's root is the parent)."""
    parent = span._parent
    while parent is not None and parent._operation_name not in _ALWAYS_EMITTING:
        parent = parent._parent
    return parent.span_id if parent is not None else None


def _mark_reported(span: _LayerLensSpan, *, replied: bool) -> None:
    """Tell every span enclosing ``span`` that it reported a model call, so none
    of them reports the same call again."""
    parent = span._parent
    while parent is not None:
        parent._call_reported_below = True
        if replied:
            parent._reply_reported_below = True
        parent = parent._parent


def _calls_reported_below(span: _LayerLensSpan, output: Any) -> bool:
    """Whether the spans beneath a generator component reported its LLM calls:
    with their replies, or (when the component has no reply of its own) as
    failures."""
    return span._reply_reported_below or (span._call_reported_below and _generator_meta(output) is None)


def _agent_name(agent_span: _LayerLensSpan) -> Optional[str]:
    """The name of the Agent a ``haystack.agent.run`` span runs, honest-guarded.

    An Agent added to a pipeline runs under its own ``haystack.component.run``
    span, whose name is the one the developer gave it. An Agent that runs on its
    own, or that another component calls from its ``run()``, has no name: the
    enclosing component's name is that component's, not the Agent's.
    """
    component = agent_span._parent
    if component is None or component._operation_name != _COMPONENT_RUN:
        return None
    if component._tags.get("haystack.component.type") != "Agent":
        return None
    return _honest_component_name(component._tags.get("haystack.component.name"))


def _enclosing_agent_name(span: _LayerLensSpan) -> Optional[str]:
    """The name of the Agent whose run ``span`` is part of, if it has one."""
    node: Optional[_LayerLensSpan] = span
    while node is not None:
        if node._operation_name == _AGENT_RUN:
            return _agent_name(node)
        node = node._parent
    return None


def _enclosing_component_name(span: _LayerLensSpan) -> Optional[str]:
    """The honest name of the pipeline component whose ``run()`` opened ``span``.

    Stops at an Agent: what runs inside an Agent belongs to the Agent, which
    ``agent_name`` names.
    """
    node = span._parent
    while node is not None:
        if node._operation_name == _AGENT_RUN:
            return None
        if node._operation_name == _COMPONENT_RUN:
            return _honest_component_name(node._tags.get("haystack.component.name"))
        node = node._parent
    return None


def _stamp_agent(payload: Dict[str, Any], name: Optional[str]) -> None:
    if name is not None:
        payload["agent_name"] = name


def _stamp_error(payload: Dict[str, Any], tags: Dict[str, Any]) -> None:
    """The failure the tracer saw leave the span: its message, which is content
    and is dropped under ``capture_content=False``, and the exception class and the
    provider's reason code, the categories that survive so the failure stays visible."""
    if not tags.get("error"):
        return
    payload["error"] = str(tags.get("error.message", "unknown"))
    error_type = tags.get("error.type")
    if error_type:
        payload["error_type"] = str(error_type)
    # The provider's reason, read off the exception as it left the span (LAY-4454).
    error_code = tags.get("error.code")
    if error_code:
        payload["error_code"] = str(error_code)


def _tool_names(tools: Any) -> List[str]:
    """The names of the tools in an Agent's ``haystack.agent.tools`` tag: a list of
    ``Tool`` and ``Toolset`` objects, or one ``Toolset`` (an iterable of ``Tool``)."""
    if tools is None:
        return []
    names: List[str] = []
    for item in tools if isinstance(tools, (list, tuple)) else [tools]:
        try:
            members = list(item)  # a Toolset iterates its tools
        except TypeError:
            members = [item]  # a Tool is not iterable
        except Exception:
            log.warning("layerlens: could not list the tools of %s", type(item).__name__, exc_info=True)
            continue
        names.extend(n for n in (getattr(m, "name", None) for m in members) if isinstance(n, str))
    return names


def _generator_meta(output: Any) -> Optional[Dict[str, Any]]:
    """The response metadata (model, usage, finish_reason) of a generator's first reply.

    Haystack generators return it in one of two shapes:

    * text generators (``OpenAIGenerator``, haystack 2.x only) return
      ``{"replies": [str], "meta": [dict]}``, one meta dict per reply;
    * chat generators (``OpenAIChatGenerator`` and the other ChatGenerators; on
      haystack 3 the only OpenAI generator) return ``{"replies": [ChatMessage]}``
      and keep it on each reply's ``ChatMessage.meta``. A top-level ``meta`` one
      of them adds (``FallbackChatGenerator``'s dict of fallback bookkeeping) is
      not it, which is why only a list counts as the text generator's.

    The first reply speaks for the call: the provider reports usage once per
    request and haystack copies it onto every reply, so summing them would count
    the same tokens once per reply.
    """
    if not isinstance(output, dict):
        return None
    meta_list = output.get("meta")
    if isinstance(meta_list, list) and meta_list and isinstance(meta_list[0], dict):
        return meta_list[0]
    replies = output.get("replies")
    if isinstance(replies, list) and replies:
        reply_meta = getattr(replies[0], "meta", None)
        if isinstance(reply_meta, dict):
            return reply_meta
    return None


def _extract_model(tags: Dict[str, Any]) -> Optional[str]:
    model = tags.get("haystack.model")
    if model:
        return str(model)
    meta = _generator_meta(tags.get("haystack.component.output"))
    if meta and meta.get("model"):
        return str(meta["model"])
    return None


def _extract_usage(output: Any) -> Optional[Dict[str, int]]:
    meta = _generator_meta(output)
    return meta.get("usage") if meta else None


def _extract_finish_reason(output: Any) -> Optional[str]:
    meta = _generator_meta(output)
    return meta.get("finish_reason") if meta else None


def _get_version() -> str:
    try:
        import haystack as _mod  # pyright: ignore[reportMissingImports]

        return getattr(_mod, "__version__", "unknown")
    except Exception:
        return "unknown"
