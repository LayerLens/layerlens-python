from __future__ import annotations

import time
import asyncio
import logging
import functools
from uuid import UUID
from typing import Any, Dict, List, Optional, Sequence

from ..._context import (
    RunState,
    _pop_span,
    _push_span,
    _current_run,
    _parent_span_id,
    _current_span_id,
    _current_collector,
    _current_span_name,
)
from ..._identity import _API_METHOD_RE, _is_generic
from ..._error_code import error_category
from ._base_framework import FrameworkAdapter
from .._tool_arguments import parse_tool_arguments
from ..._capture_config import CaptureConfig
from ._langchain_memory import TracedMemory, MemoryMutationTracker, wrap_memory
from ....attestation._hash import canonical_json

__all__ = [
    "LangChainCallbackHandler",
    "MemoryMutationTracker",
    "TracedMemory",
    "wrap_memory",
]


log = logging.getLogger(__name__)

#: How long the run of a model called on its own stays open after the task the call started in has
#: finished without the call's end: long enough for an end LangChain shields from a cancellation.
_UNENDED_RUN_GRACE_SECONDS = 60.0

#: Past this many open runs, a run with no callback for ``_STALE_RUN_SECONDS`` is dropped. A call
#: cancelled inside a task that goes on (``asyncio.timeout``, ``wait_for`` on Python 3.12+) never ends
#: its run, and its task does not end either; nothing LangChain reports tells it from a call still
#: waiting. A run's age counts from its last callback, so a call that streams stays; and an hour is
#: past the OpenAI and Anthropic SDKs' default request timeout (600 s) with their two retries.
_MAX_OPEN_MODEL_RUNS = 256
_STALE_RUN_SECONDS = 3600.0


def _model_run_scope(fn):  # type: ignore[type-arg]
    """Decorator for the LLM callbacks: a model called on its own runs in a run of its own, attached
    to the context only while one of its callbacks runs.

    An async model reports its end (``agenerate`` → ``on_llm_end``) from an ``asyncio.gather`` task,
    a copy of the caller's context, where the run could end but the caller's ContextVars could not be
    reset: the next call in that context reused the ended run, never uploaded, and provider adapters
    took it for the current trace. So the run is kept by its run id, attached for each of its
    callbacks and detached after; its end, ``@_auto_flush``, ends it while attached.
    """

    @functools.wraps(fn)
    def wrapper(self, *args, run_id, parent_run_id=None, **kwargs):  # type: ignore[no-untyped-def]
        key = str(run_id)
        run = self._model_runs.get(key)
        if run is None:
            if fn.__name__ not in ("on_llm_start", "on_chat_model_start") or parent_run_id is not None:
                return fn(self, *args, run_id=run_id, parent_run_id=parent_run_id, **kwargs)
            run = self._begin_langchain_run()
            run.data["root_run_id"] = key
            run.data["owns_collector"] = run._col_token is not None
            self._keep_model_run(key, run)
        else:
            self._saw_model_run(key, run)
            self._attach(run)
        try:
            return fn(self, *args, run_id=run_id, parent_run_id=parent_run_id, **kwargs)
        finally:
            if _current_run.get() is run:
                self._detach(run)
            else:  # ended by the callback (@_auto_flush)
                self._release_model_run(key)

    return wrapper


def _auto_flush(fn):  # type: ignore[type-arg]
    """Decorator: after the callback returns, flush if this was the outermost run."""

    @functools.wraps(fn)
    def wrapper(self, *args, run_id, **kwargs):  # type: ignore[no-untyped-def]
        fn(self, *args, run_id=run_id, **kwargs)
        run = self._get_run()
        if run is not None and str(run_id) == run.data.get("root_run_id"):
            # Marked first: an async run ends in a copy of the context its start set (see
            # _begin_langchain_run), where the caller's ContextVars cannot be reset.
            run.data["ended"] = True
            self._end_run()

    return wrapper


try:
    # fmt: off
    from langchain_core.messages import BaseMessage
    from langchain_core.callbacks import BaseCallbackHandler  # pyright: ignore[reportAssignmentType]
    # fmt: on
except ImportError:
    BaseMessage = ()  # type: ignore[assignment] # isinstance(x, ()) is always False

    class BaseCallbackHandler:  # type: ignore[no-redef]
        def __init_subclass__(cls, **kwargs: Any) -> None:
            raise ImportError(
                "The 'langchain-core' package is required for LangChain instrumentation. "
                'Install it with: uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[langchain]"'
            )


# Recursion guard for pathological/cyclic state. Graph state is shallow in
# practice; this only bounds adversarial inputs.
_MAX_JSONABLE_DEPTH = 25


def _to_jsonable(obj: Any, _depth: int = 0) -> Any:
    """Convert framework state into a JSON-safe structure for trace payloads.

    LangGraph threads the graph state — typically full of LangChain message
    objects — through the chain callbacks. Those objects are not JSON
    serializable, so embedding them raw would make the attestation hash chain
    raise and silently drop the event (``agent.input`` / ``agent.node.enter`` /
    ``agent.handoff``). We serialize messages the same way the ``model.invoke``
    path does (:func:`_serialize_lc_message`) and recurse through dicts/lists.

    Values that already serialize are returned **unchanged**, so attestation
    hashes are identical for the common, already-clean case.
    """
    if _depth > _MAX_JSONABLE_DEPTH:
        return repr(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, BaseMessage):
        data: Dict[str, Any] = {
            "type": getattr(obj, "type", None),
            "content": _to_jsonable(getattr(obj, "content", None), _depth + 1),
        }
        tool_calls = getattr(obj, "tool_calls", None)
        if tool_calls:
            data["tool_calls"] = _to_jsonable(tool_calls, _depth + 1)
        return data
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v, _depth + 1) for v in obj]
    # Leaf object: keep it if the attestation serializer already handles it
    # (datetime / Enum / dataclass / to_dict), otherwise fall back to a string
    # so we never drop the event.
    try:
        canonical_json(obj)
        return obj
    except TypeError:
        return str(obj)


# LangChain composition primitives + legacy chain/agent classes + LCEL component
# class defaults. When a runnable is invoked WITHOUT a developer-declared
# ``run_name``, LangChain passes the runnable's ``get_name()`` (a class default
# like ``RunnableSequence``, ``AgentExecutor``, or the class of the prompt/parser
# component running as a chain step — ``ChatPromptTemplate``, ``StrOutputParser``)
# as the ``name`` kwarg to ``on_chain_start``. That is plumbing, never a
# producer-declared agent identity — surfacing it in the Agent column is a
# fabrication, so it must stay honestly blank. All of LangChain's composition
# wrappers live under the ``Runnable*`` prefix (handled separately); this is the
# precise denylist of the remaining legacy chain/agent + LCEL component class
# defaults. Model component class defaults (``ChatOpenAI`` ...) are rejected by
# the shared ``_is_generic`` guard, and models run through the on_(chat_)model
# path rather than on_chain_start, so they never reach this list as an agent
# identity anyway.
_LANGCHAIN_CLASS_DEFAULTS = frozenset(
    {
        # legacy chain / agent class defaults
        "agentexecutor",
        "llmchain",
        "conversationchain",
        "conversationalretrievalchain",
        "retrievalqa",
        "retrievalqawithsourceschain",
        "stuffdocumentschain",
        "mapreducedocumentschain",
        "refinedocumentschain",
        "sequentialchain",
        "simplesequentialchain",
        "transformchain",
        # LCEL prompt-template component class defaults
        "prompttemplate",
        "chatprompttemplate",
        "humanmessageprompttemplate",
        "systemmessageprompttemplate",
        "aimessageprompttemplate",
        "chatmessageprompttemplate",
        "messagesplaceholder",
        "fewshotprompttemplate",
        "fewshotchatmessageprompttemplate",
        "pipelineprompttemplate",
        "imageprompttemplate",
        # LCEL output-parser component class defaults
        "stroutputparser",
        "jsonoutputparser",
        "simplejsonoutputparser",
        "pydanticoutputparser",
        "listoutputparser",
        "commaseparatedlistoutputparser",
        "markdownlistoutputparser",
        "numberedlistoutputparser",
        "xmloutputparser",
        "structuredoutputparser",
        "booleanoutputparser",
        "datetimeoutputparser",
        "enumoutputparser",
        "regexparser",
        "outputfixingparser",
        "retryoutputparser",
        "jsonoutputkeytoolsparser",
        "pydantictoolsparser",
    }
)


def _honest_run_name(raw: Any) -> Optional[str]:
    """Return a developer-DECLARED LCEL run name honest for the Agent column, else None.

    LangChain surfaces the ``run_name`` a developer set via
    ``.with_config(run_name=...)`` as the ``name`` kwarg on ``on_chain_start``.
    When no run_name was declared, the same kwarg carries the runnable's
    ``get_name()`` class default (``RunnableSequence``, ``RunnableParallel<a,b>``,
    ``AgentExecutor``, ...). We surface ONLY a genuine, distinctive developer name
    and reject every class default / generic placeholder / dotted API-method label,
    reusing the shared identity guard so this adapter never fabricates a node.
    A run genuinely without a declared name stays honestly blank.
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name:
        return None
    # ``RunnableParallel<first,second>`` -> ``RunnableParallel`` before matching.
    base = name.split("<", 1)[0].strip()
    low = base.lower()
    if low.startswith("runnable") or low in _LANGCHAIN_CLASS_DEFAULTS:
        return None
    if _is_generic(name) or _API_METHOD_RE.match(name.lower()):
        return None
    return name


class LangChainCallbackHandler(BaseCallbackHandler, FrameworkAdapter):
    name = "langchain"

    #: Called in the run's own context in an async run. Otherwise LangChain calls a sync handler's
    #: callbacks there in an executor thread, each in a copy of the context: the run begun at the
    #: root's start lived in one copy, the callbacks after it saw none, and `ainvoke` / `astream`
    #: recorded nothing. The callbacks are quick and never wait on I/O (an upload is queued for
    #: the upload worker), so they do not hold up the event loop.
    run_inline = True

    def __init__(self, client: Any, capture_config: Optional[CaptureConfig] = None) -> None:
        BaseCallbackHandler.__init__(self)
        FrameworkAdapter.__init__(self, client, capture_config=capture_config)
        # Pending LLM runs: run_id -> {name, messages, parent_run_id, tokens_accum, first_token_at_ns}
        self._pending_llm: Dict[str, Dict[str, Any]] = {}
        # run_id -> producer-declared honest agent_name for a chain run. Populated
        # from the LCEL run_name in on_chain_start and inherited by sub-chains /
        # child model+tool runs so they attribute to the same honest node. Cleared
        # in on_chain_end / on_chain_error.
        self._chain_agent_names: Dict[str, str] = {}
        # run_id -> the open run of a model called on its own (see _model_run_scope).
        self._model_runs: Dict[str, RunState] = {}

    def _begin_langchain_run(self) -> RunState:
        """``_begin_run`` for a root run, after putting back what an ended run left in the context.

        LangChain reports an async run's end (``on_chain_end``, ``on_llm_end``, ``on_retriever_end``
        and their errors, all ``@shielded``) from a task of its own, with a copy of the caller's
        context: the run ends there, and the ContextVars its start set in the caller's context stay
        set. The caller's next run in that task joined the ended run and its sealed collector, and
        recorded nothing (L8 B04). A root's start runs in the caller's context, so it first puts back
        what each ended run found there when it began, then begins its own.
        """
        ended = _current_run.get()
        while ended is not None and ended.data.get("ended") and "outer" in ended.data:
            collector, span_id, parent_span_id, span_name, outer_run = ended.data["outer"]
            _current_collector.set(collector)
            _current_span_id.set(span_id)
            _parent_span_id.set(parent_span_id)
            _current_span_name.set(span_name)
            _current_run.set(outer_run)
            ended = outer_run
        outer = (
            _current_collector.get(),
            _current_span_id.get(),
            _parent_span_id.get(),
            _current_span_name.get(),
            _current_run.get(),
        )
        run = self._begin_run()
        run.data["outer"] = outer
        return run

    def _keep_model_run(self, key: str, run: RunState) -> None:
        """Keep ``run`` open until its end, however many calls are in flight.

        A call's end fails to come only when its task is cancelled: ``agenerate`` re-raises the
        cancellation from its ``gather`` without ``on_llm_error``. So when the task the call started
        in finishes with the run still open, the run is dropped ``_UNENDED_RUN_GRACE_SECONDS`` later,
        unless an end shielded from that cancellation has come by then. A sync call's end always
        comes, in its own thread, so it needs no watch. Past ``_MAX_OPEN_MODEL_RUNS``, a run with no
        callback for ``_STALE_RUN_SECONDS`` is dropped too, for a call cancelled in a task that goes on.
        """
        run.data["seen_at"] = time.monotonic()
        self._model_runs[key] = run
        if len(self._model_runs) > _MAX_OPEN_MODEL_RUNS:
            self._drop_stale_model_runs(but=key)
        try:
            task = asyncio.current_task()
        except RuntimeError:  # no running event loop
            task = None
        if task is None:
            return

        def task_done(done: "asyncio.Task[Any]") -> None:
            done.get_loop().call_later(_UNENDED_RUN_GRACE_SECONDS, self._drop_unended_model_run, key, run)

        task.add_done_callback(task_done)
        run.data["task_watch"] = (task, task_done)

    def _saw_model_run(self, key: str, run: RunState) -> None:
        """Note a callback of ``run``: its age starts again, and it moves to the end of the dict, which
        stays in the order of each run's last callback."""
        run.data["seen_at"] = time.monotonic()
        self._model_runs.pop(key, None)
        self._model_runs[key] = run

    def _release_model_run(self, key: str) -> None:
        """Forget an ended model run, and stop watching the task it started in."""
        run = self._model_runs.pop(key, None)
        watch = run.data.pop("task_watch", None) if run is not None else None
        if watch is not None:
            task, task_done = watch
            task.remove_done_callback(task_done)

    def _drop_stale_model_runs(self, but: str) -> None:
        """Drop the runs with no callback for ``_STALE_RUN_SECONDS``, longest first (the dict's order),
        except ``but``, the run just kept. A run another thread's call ended meanwhile is skipped."""
        now = time.monotonic()
        for key in list(self._model_runs):
            run = self._model_runs.get(key)
            if run is None:
                continue
            if key == but or now - run.data["seen_at"] < _STALE_RUN_SECONDS:
                break
            self._release_model_run(key)
            log.warning(
                "layerlens: dropped the open trace of model run %s: no callback for %d s", key, int(_STALE_RUN_SECONDS)
            )

    def _drop_unended_model_run(self, key: str, run: RunState) -> None:
        if self._model_runs.get(key) is run:
            del self._model_runs[key]
            log.warning("layerlens: dropped the open trace of model run %s: its task ended, its run did not", key)

    def _attach(self, run: RunState) -> None:
        """Make ``run`` the context's current run, as ``_begin_run`` made it, until ``_detach``."""
        if run.data.get("owns_collector"):
            run._col_token = _current_collector.set(run.collector)
        run._span_snapshot = _push_span(run.root_span_id, f"{self.name}:root")
        run._token = _current_run.set(run)

    @staticmethod
    def _detach(run: RunState) -> None:
        """Undo ``_attach`` (or ``_begin_run``) in this context, leaving the run open."""
        _current_run.reset(run._token)
        _pop_span(run._span_snapshot)
        if run._col_token is not None:
            _current_collector.reset(run._col_token)
            run._col_token = None

    def _begin_root_run(self, run_id: UUID, parent_run_id: Optional[UUID]) -> None:
        """Begin a run for a chain, a tool or a retriever LangChain runs with no parent run.

        A tool or a retriever invoked on its own is a root run, as a chain is: outside a ``trace`` /
        ``trace_context`` block nothing else begins a trace for it, and it recorded nothing (L3
        AD-22). Its end or error, the root run's, ends the run (``@_auto_flush``); inside a block the
        run shares the block's trace, which the block uploads. Their ends come in the context their
        start ran in. A model's may not, so a model called on its own has ``_model_run_scope``.
        """
        if parent_run_id is None:
            run = self._begin_langchain_run()
            run.data["root_run_id"] = str(run_id)

    # ------------------------------------------------------------------
    # Chain callbacks
    # ------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: Optional[Dict[str, Any]],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._begin_root_run(run_id, parent_run_id)
        serialized = serialized or {}
        name = serialized.get("name") or serialized.get("id", ["unknown"])[-1]
        payload = self._payload(name=name)
        # Honest node identity: a developer-declared LCEL run_name (the ``name``
        # kwarg) — never a class default. Inherit the enclosing named chain's
        # identity for sub-chains so nested runs attribute to the same node.
        agent_name = _honest_run_name(kwargs.get("name"))
        if agent_name is None and parent_run_id is not None:
            agent_name = self._chain_agent_names.get(str(parent_run_id))
        if agent_name is not None:
            self._chain_agent_names[str(run_id)] = agent_name
            payload["agent_name"] = agent_name
        self._set_if_capturing(payload, "input", _to_jsonable(inputs))
        self._emit("agent.input", payload, run_id=run_id, parent_run_id=parent_run_id)

    @_auto_flush
    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        payload = self._payload(status="ok")
        agent_name = self._chain_agent_names.pop(str(run_id), None)
        if agent_name is not None:
            payload["agent_name"] = agent_name
        self._set_if_capturing(payload, "output", _to_jsonable(outputs))
        self._emit("agent.output", payload, run_id=run_id, parent_run_id=parent_run_id)

    @_auto_flush
    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        payload = self._payload(error=str(error), **error_category(error), status="error")
        agent_name = self._chain_agent_names.pop(str(run_id), None)
        if agent_name is not None:
            payload["agent_name"] = agent_name
        self._emit(
            "agent.error",
            payload,
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    # ------------------------------------------------------------------
    # LLM callbacks — merged into single model.invoke on end
    # ------------------------------------------------------------------

    @_model_run_scope
    def on_llm_start(
        self,
        serialized: Optional[Dict[str, Any]],
        prompts: List[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        serialized = serialized or {}
        name = serialized.get("name") or serialized.get("id", ["unknown"])[-1]
        self._start_timer(str(run_id))
        pending: Dict[str, Any] = {
            "name": name,
            "parent_run_id": parent_run_id,
            "agent_name": self._chain_agent_names.get(str(parent_run_id)) if parent_run_id else None,
            "start_ns": time.time_ns(),
        }
        self._set_if_capturing(pending, "messages", prompts)
        self._pending_llm[str(run_id)] = pending

    @_model_run_scope
    def on_chat_model_start(
        self,
        serialized: Optional[Dict[str, Any]],
        messages: List[List[Any]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        serialized = serialized or {}
        name = serialized.get("name") or serialized.get("id", ["unknown"])[-1]
        self._start_timer(str(run_id))
        pending: Dict[str, Any] = {
            "name": name,
            "parent_run_id": parent_run_id,
            "agent_name": self._chain_agent_names.get(str(parent_run_id)) if parent_run_id else None,
            "start_ns": time.time_ns(),
        }
        self._set_if_capturing(
            pending,
            "messages",
            [[_serialize_lc_message(m) for m in batch] for batch in messages],
        )
        self._pending_llm[str(run_id)] = pending

    @_model_run_scope
    def on_llm_new_token(
        self,
        token: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        """Accumulate streaming tokens; captures time-to-first-token per run."""
        pending = self._pending_llm.get(str(run_id))
        if pending is None:
            return
        if pending.get("first_token_at_ns") is None:
            pending["first_token_at_ns"] = time.time_ns()
        pending["tokens_accum"] = (pending.get("tokens_accum") or 0) + 1
        if self._config.capture_content:
            pending["streamed_text"] = (pending.get("streamed_text") or "") + (token or "")

    @_model_run_scope
    @_auto_flush
    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        pending = self._pending_llm.pop(str(run_id), {})

        # Extract response data
        output = None
        finish_reason = None
        reply: Any = None
        tool_calls: list[dict[str, Any]] = []
        try:
            generations = response.generations
            if generations and generations[0]:
                gen0 = generations[0][0]
                raw_output = getattr(gen0, "text", None)
                output = raw_output if isinstance(raw_output, str) else None
                gen_info = getattr(gen0, "generation_info", None)
                if not isinstance(gen_info, dict):
                    gen_info = {}
                fr = gen_info.get("finish_reason")
                finish_reason = fr if isinstance(fr, str) else None
                msg = getattr(gen0, "message", None)
                if msg is not None:
                    reply = msg
                    tool_calls = _requested_tool_calls(msg)
        except (AttributeError, IndexError):
            pass

        try:
            llm_output = response.llm_output
        except AttributeError:
            llm_output = None
        if not isinstance(llm_output, dict):
            llm_output = {}

        # A streamed call has no llm_output: LangChain merges its chunks into one message, whose
        # response_metadata names the model and whose usage_metadata (LangChain's standard usage
        # field) holds the tokens. Reading llm_output alone recorded such a call without either,
        # and with no cost.record (L7 AD-05).
        raw_model = llm_output.get("model_name")
        model_name = raw_model if isinstance(raw_model, str) else _reply_model_name(reply)

        # Build single merged model.invoke event
        payload = self._payload()
        if pending.get("name"):
            payload["name"] = pending["name"]
        if pending.get("agent_name"):
            payload["agent_name"] = pending["agent_name"]
        if model_name:
            payload["model"] = model_name
        self._set_if_capturing(payload, "messages", pending.get("messages"))
        self._set_if_capturing(payload, "output_message", output)

        # Latency
        latency_ms = self._stop_timer(str(run_id))
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms

        # Streaming metrics — time-to-first-token + chunk count
        first_tok = pending.get("first_token_at_ns")
        if first_tok is not None:
            payload["streaming"] = True
            payload["streamed_chunks"] = pending.get("tokens_accum", 0)
            start_ns = pending.get("start_ns")
            if first_tok and start_ns:
                payload["ttft_ms"] = (first_tok - start_ns) / 1e6

        if finish_reason is not None:
            payload["finish_reason"] = finish_reason

        # Tokens
        # Cached input is priced at the full input rate here, as for llm_output's token_usage:
        # input_tokens / prompt_tokens include cache reads, and the shared framework pricing
        # (_price_cost_record) does not read a cached count, for this adapter or any other.
        usage = llm_output.get("token_usage") or llm_output.get("usage_metadata") or _reply_usage(reply)
        tokens = self._normalize_tokens(usage)
        payload.update(tokens)

        self._emit(
            "model.invoke",
            payload,
            run_id=run_id,
            parent_run_id=pending.get("parent_run_id"),
        )

        # Emit tool.call events for any tool calls the model requested
        for tc in tool_calls:
            tc_payload = self._payload(id=tc["id"], tool_name=tc["tool_name"])
            if model_name:
                tc_payload["model"] = model_name
            # Before langchain-openai 1.7 the arguments are the provider's JSON string
            # (additional_kwargs); from 1.7, langchain's parsed dict (message.tool_calls),
            # or the raw string when they are not valid JSON (message.invalid_tool_calls).
            # Emit the object either way, as the provider adapters do; a string that does
            # not decode stays a string. They are content, and parsing logs a snippet of
            # a malformed string, so with capture_content off they are not even read.
            arguments = parse_tool_arguments(tc["arguments"]) if self._config.capture_content else None
            self._set_if_capturing(tc_payload, "arguments", arguments)
            self._emit(
                "tool.call",
                tc_payload,
                run_id=run_id,
                parent_run_id=pending.get("parent_run_id"),
            )

        # Separate cost.record if we have token data
        if tokens:
            cost_payload = self._payload()
            if model_name:
                cost_payload["model"] = model_name
            cost_payload.update(tokens)
            self._emit(
                "cost.record",
                cost_payload,
                run_id=run_id,
                parent_run_id=pending.get("parent_run_id"),
            )

    @_model_run_scope
    @_auto_flush
    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        pending = self._pending_llm.pop(str(run_id), {})

        payload = self._payload(error=str(error))
        if pending.get("name"):
            payload["name"] = pending["name"]
        if pending.get("agent_name"):
            payload["agent_name"] = pending["agent_name"]
        latency_ms = self._stop_timer(str(run_id))
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms
        self._emit(
            "model.invoke",
            payload,
            run_id=run_id,
            parent_run_id=pending.get("parent_run_id"),
        )

        self._emit(
            "agent.error",
            self._payload(error=str(error), **error_category(error), status="error"),
            run_id=run_id,
            parent_run_id=pending.get("parent_run_id"),
        )

    # ------------------------------------------------------------------
    # Tool callbacks
    # ------------------------------------------------------------------

    def on_tool_start(
        self,
        serialized: Optional[Dict[str, Any]],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._begin_root_run(run_id, parent_run_id)
        name = (serialized or {}).get("name", "tool")
        payload = self._payload(name=name)
        self._set_if_capturing(payload, "input", _to_jsonable(input_str))
        self._emit("tool.call", payload, run_id=run_id, parent_run_id=parent_run_id)

    @_auto_flush
    def on_tool_end(
        self,
        output: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        payload = self._payload()
        self._set_if_capturing(payload, "output", output)
        self._emit("tool.result", payload, run_id=run_id, parent_run_id=parent_run_id)

    @_auto_flush
    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._emit(
            "agent.error",
            self._payload(error=str(error), **error_category(error), status="error"),
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    # ------------------------------------------------------------------
    # Retriever callbacks
    # ------------------------------------------------------------------

    def on_retriever_start(
        self,
        serialized: Optional[Dict[str, Any]],
        query: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._begin_root_run(run_id, parent_run_id)
        name = (serialized or {}).get("name", "retriever")
        payload = self._payload(name=name)
        self._set_if_capturing(payload, "input", query)
        self._emit("tool.call", payload, run_id=run_id, parent_run_id=parent_run_id)

    @_auto_flush
    def on_retriever_end(
        self,
        documents: Sequence[Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        payload = self._payload()
        self._set_if_capturing(
            payload,
            "output",
            [_serialize_lc_document(d) for d in documents],
        )
        self._emit("tool.result", payload, run_id=run_id, parent_run_id=parent_run_id)

    @_auto_flush
    def on_retriever_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        self._emit(
            "agent.error",
            self._payload(error=str(error), **error_category(error), status="error"),
            run_id=run_id,
            parent_run_id=parent_run_id,
        )

    # ------------------------------------------------------------------
    # Agent callbacks
    # ------------------------------------------------------------------

    def on_agent_action(
        self,
        action: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        payload = self._payload(tool=getattr(action, "tool", "unknown"))
        self._set_if_capturing(payload, "tool_input", _to_jsonable(getattr(action, "tool_input", None)))
        self._set_if_capturing(payload, "log", getattr(action, "log", None) or None)
        self._emit("agent.input", payload, run_id=run_id, parent_run_id=parent_run_id)

    # Not @_auto_flush: LangChain reports an agent's finish with the run id of the chain running
    # the agent, the root run for a top-level AgentExecutor, and that chain's end follows. Ending
    # the trace here dropped the root chain's agent.output (L7 AD-04); the chain's end ends it.
    def on_agent_finish(
        self,
        finish: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        payload = self._payload(status="ok")
        self._set_if_capturing(payload, "output", _to_jsonable(getattr(finish, "return_values", None)))
        self._set_if_capturing(payload, "log", getattr(finish, "log", None) or None)
        self._emit("agent.output", payload, run_id=run_id, parent_run_id=parent_run_id)

    # ------------------------------------------------------------------
    # No-ops (required by base)
    # ------------------------------------------------------------------

    def on_text(self, text: str, **kwargs: Any) -> None:
        pass


def _requested_tool_calls(msg: Any) -> List[Dict[str, Any]]:
    """The tool calls a chat model's reply requests, as ``id`` / ``tool_name`` / ``arguments``.

    langchain-openai before 1.7 copies OpenAI's raw ``tool_calls`` into
    ``additional_kwargs``: every call the model made, ``arguments`` still the JSON
    string. From 1.7 that copy is gone, and the calls are only in langchain's own
    lists: ``tool_calls`` for those whose arguments parsed (``args`` is a dict) and
    ``invalid_tool_calls`` for those that did not (``args`` is the raw string). Reading
    ``tool_calls`` alone dropped every malformed call from the trace, so both are read.
    """
    extra = getattr(msg, "additional_kwargs", None)
    raw_calls = extra.get("tool_calls") if isinstance(extra, dict) else None
    from_raw_calls = isinstance(raw_calls, (list, tuple)) and len(raw_calls) > 0
    calls = raw_calls if from_raw_calls else getattr(msg, "tool_calls", None)
    requested: List[Dict[str, Any]] = []
    for tc in calls if isinstance(calls, (list, tuple)) else []:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            requested.append(
                {
                    "id": tc.get("id"),
                    "tool_name": fn.get("name") or tc.get("name"),
                    "arguments": fn.get("arguments") or tc.get("args"),
                }
            )

    # langchain-core derives invalid_tool_calls from the raw list too, so before 1.7 a
    # malformed call is in both. Skip it there: one call is one tool.call. It is matched
    # by id; a call without one can't be, but the raw list holds every call the model
    # made, so when the raw list was read it is already in.
    listed_ids = {call["id"] for call in requested if call["id"] is not None}
    invalid_calls = getattr(msg, "invalid_tool_calls", None)
    for itc in invalid_calls if isinstance(invalid_calls, (list, tuple)) else []:
        if not isinstance(itc, dict):
            continue
        call_id = itc.get("id")
        already_listed = from_raw_calls if call_id is None else call_id in listed_ids
        if not already_listed:
            requested.append({"id": call_id, "tool_name": itc.get("name"), "arguments": itc.get("args")})
    return requested


def _reply_usage(reply: Any) -> Optional[Dict[str, Any]]:
    """The reply message's ``usage_metadata`` (input/output/total tokens), or None."""
    usage = getattr(reply, "usage_metadata", None)
    return dict(usage) if isinstance(usage, dict) and usage else None


def _reply_model_name(reply: Any) -> Optional[str]:
    """The model the reply message's ``response_metadata`` names, or None."""
    metadata = getattr(reply, "response_metadata", None)
    if not isinstance(metadata, dict):
        return None
    for key in ("model_name", "model"):
        name = metadata.get(key)
        if isinstance(name, str) and name:
            return name
    return None


def _serialize_lc_message(msg: Any) -> Any:
    try:
        return {"type": msg.type, "content": msg.content}
    except AttributeError:
        return str(msg)


def _serialize_lc_document(doc: Any) -> Any:
    try:
        return {"page_content": doc.page_content, "metadata": doc.metadata}
    except AttributeError:
        return str(doc)
