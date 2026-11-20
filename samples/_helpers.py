"""Shared helpers for LayerLens SDK samples.

Provides utility functions used across multiple samples to keep
individual sample files focused on demonstrating SDK features.
"""

from __future__ import annotations

import os
import json
import time
import logging
import tempfile
from typing import Any, List, Optional

from layerlens import LayerLens, NotFoundError

logger = logging.getLogger(__name__)


def upload_trace_dict(
    client: LayerLens,
    *,
    input_text: str,
    output_text: str,
    metadata: Optional[dict[str, Any]] = None,
) -> Any:
    """Upload a single trace from in-memory data.

    Writes the trace to a temporary JSONL file and uploads via the SDK's
    ``client.traces.upload()`` method.

    Args:
        client: An initialized :class:`LayerLens` client.
        input_text: The input/prompt text for the trace.
        output_text: The output/response text for the trace.
        metadata: Optional metadata dict attached to the trace.

    Returns:
        A :class:`CreateTracesResponse` with ``trace_ids``.
    """
    trace_data: dict[str, Any] = {
        "input": [{"role": "user", "content": input_text}],
        "output": output_text,
    }
    if metadata:
        trace_data["metadata"] = metadata

    fd, path = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(trace_data) + "\n")
        result = client.traces.upload(path)
    finally:
        if os.path.exists(path):
            os.unlink(path)

    return result


def recorded_trace_path(*parts: str) -> str:
    """Resolve a shipped recorded-trace fixture under ``samples/data/traces/``.

    The industry/cowork samples upload **recorded real traces** — genuine,
    fully instrumented traces captured once (by ``data/_generate_fixtures.py``)
    from real model runs and shipped in the repo. Passing the path parts here
    keeps the samples portable regardless of the working directory.

    Example::

        path = recorded_trace_path("industry", "financial_fraud.jsonl")
    """
    return os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "traces", *parts)
    )


def upload_recorded_trace(client: LayerLens, fixture_path: str) -> List[str]:
    """Upload a recorded real-trace fixture and return the created trace IDs.

    A fixture is a JSONL file where each line is a complete, honestly
    attested trace captured from a real instrumented agent run (see
    ``samples/data/_generate_fixtures.py``). Because the trace carries real
    ``agent.identity``/``model.invoke``/``agent.output`` events, the LayerLens
    UI renders the Agent, Framework, and Status columns from genuine data — no
    fabrication. The returned IDs are in file order, so callers can zip them
    back onto the source scenarios to run evaluations.

    Args:
        client: An initialized :class:`LayerLens` client.
        fixture_path: Absolute path to the ``.jsonl`` fixture (see
            :func:`recorded_trace_path`).

    Returns:
        The created trace IDs in file order (empty list if the upload was
        rejected without raising).
    """
    with open(fixture_path) as f:
        traces = [json.loads(line) for line in f if line.strip()]
    if not traces:
        return []

    # Upload the traces together as a JSON array so the backend creates one
    # trace per record (a JSONL file whose lines each start with "{" is read as
    # a single JSON object, yielding only the first record). The array is
    # created in-order, so the returned IDs line up with the fixture records.
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as tmp:
            json.dump(traces, tmp, default=str)
        result = client.traces.upload(path)
    finally:
        if os.path.exists(path):
            os.unlink(path)

    if result and getattr(result, "trace_ids", None):
        return list(result.trace_ids)
    return []


def trace_call(
    client: LayerLens,
    *,
    agent_name: str,
    run_fn: Any,
    input_value: Any = None,
) -> tuple[Any, Optional[str]]:
    """Trace a real, instrumented model call and upload the resulting trace.

    Use this in the integration samples to demonstrate **live tracing**: first
    ``instrument_openai(...)`` / ``instrument_anthropic(...)`` to wire the
    provider, then call ``trace_call`` with a ``run_fn`` that makes the real
    API call. The provider's instrumented call emits genuine
    ``model.invoke``/``cost.record`` events into the trace, so the uploaded
    trace carries the real framework, model, token counts, and status — nothing
    is fabricated.

    The trace is uploaded synchronously (via :meth:`traces.upload`) so the
    created trace ID is returned to the caller for running evaluations.

    Args:
        client: An initialized :class:`LayerLens` client.
        agent_name: Honest name for the agent being traced (fills the Agent
            column). It is re-verified server-side.
        run_fn: A zero-argument callable that makes the instrumented API call
            and returns its result (e.g. the completion text).
        input_value: Optional value recorded as the trace's input.

    Returns:
        ``(result, trace_id)`` where ``result`` is ``run_fn()``'s return value
        and ``trace_id`` is the created trace ID (``None`` if the upload was
        rejected).
    """
    import uuid

    from layerlens.instrument import TraceCollector
    from layerlens.instrument._capture_config import CaptureConfig
    from layerlens.instrument._context import _current_collector, _push_span, _pop_span

    collector = TraceCollector(client, CaptureConfig.full())
    root_span_id = uuid.uuid4().hex[:16]
    col_token = _current_collector.set(collector)
    span_snapshot = _push_span(root_span_id, agent_name)
    try:
        collector.emit(
            "agent.input",
            {"name": agent_name, "input": input_value},
            span_id=root_span_id,
            span_name=agent_name,
        )
        result = run_fn()
        collector.emit(
            "agent.output",
            {"name": agent_name, "output": result, "status": "ok"},
            span_id=root_span_id,
            span_name=agent_name,
        )
        # Declare the agent identity so the Agent column renders (re-verified
        # server-side; a generic/model name would be rejected).
        collector.emit(
            "agent.identity",
            {"agent_name": agent_name},
            span_id=root_span_id,
            span_name=agent_name,
        )
    finally:
        _pop_span(span_snapshot)
        _current_collector.reset(col_token)

    payload = collector.to_replay_dict()
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload, default=str) + "\n")
        upload_result = client.traces.upload(path)
    finally:
        if os.path.exists(path):
            os.unlink(path)

    trace_id = None
    if upload_result and getattr(upload_result, "trace_ids", None):
        trace_id = upload_result.trace_ids[0]
    return result, trace_id


#: The model a sample's grader runs on when the sample names none: an inexpensive one. The public
#: catalogue is ordered by key, so its first model can be a frontier model that costs many times
#: more for the same grading.
DEFAULT_JUDGE_MODEL_KEY = "openai/gpt-4o-mini"


def get_default_model_id(client: LayerLens) -> str:
    """Get a model ID suitable for judge creation.

    Takes :data:`DEFAULT_JUDGE_MODEL_KEY` from the public catalog when it is there, else the
    catalog's first model, else the project's first model.
    Caches the result for the lifetime of the process.

    Args:
        client: An initialized :class:`LayerLens` client.

    Returns:
        A model ID string suitable for passing to ``judges.create(model_id=...)``.

    Raises:
        RuntimeError: If no models are available in the project or public catalog.
    """
    # Check cache
    cached = getattr(get_default_model_id, "_cached_id", None)
    if cached:
        return cached

    # The inexpensive default, when the catalog has it
    try:
        default_resp = client.public.models.get(key=DEFAULT_JUDGE_MODEL_KEY)
        for model in getattr(default_resp, "models", None) or []:
            if getattr(model, "key", None) == DEFAULT_JUDGE_MODEL_KEY:
                get_default_model_id._cached_id = model.id  # type: ignore[attr-defined]
                return model.id
    except Exception:
        pass

    # Use public models (required for judge creation)
    try:
        public_resp = client.public.models.get()
        if public_resp and hasattr(public_resp, "models") and public_resp.models:
            get_default_model_id._cached_id = public_resp.models[0].id  # type: ignore[attr-defined]
            return public_resp.models[0].id
    except Exception:
        pass

    # Fall back to project models
    try:
        models = client.models.get()
        if models:
            get_default_model_id._cached_id = models[0].id  # type: ignore[attr-defined]
            return models[0].id
    except Exception:
        pass

    raise RuntimeError(
        "No models available. Add a model to your project or check API connectivity."
    )


# The API refuses a judge name longer than this (400 "judge name must not exceed 64 characters").
MAX_JUDGE_NAME_CHARS = 64


def create_judge(
    client: LayerLens,
    *,
    name: str,
    evaluation_goal: str,
    model_id: Optional[str] = None,
    namespace: Optional[str] = None,
    created: Optional[List[str]] = None,
) -> Any:
    """Create a judge, automatically resolving model_id if not provided.

    Args:
        client: An initialized :class:`LayerLens` client.
        name: Judge display name.
        evaluation_goal: What the judge evaluates (min 10 characters).
        model_id: Explicit model ID. If ``None``, resolves via :func:`get_default_model_id`.
        namespace: Optional per-sample namespace appended to the display name
            (``"<name> (<namespace>)"``). Judges are matched/reused by name, so
            two samples that both want e.g. ``"Relevance Judge"`` would otherwise
            silently cross-wire if a run is interrupted before cleanup. Passing
            the sample's own namespace keeps every sample's judges distinct. The
            whole name must fit in 64 characters.
        created: Optional list the judge's id is appended to as soon as the judge
            exists, so a ``finally`` block can delete every judge made before a
            later step failed.

    Returns:
        A :class:`Judge` object.

    Raises:
        ValueError: The name, with its namespace, is longer than 64 characters.
    """
    if namespace:
        name = f"{name} ({namespace})"
    if len(name) > MAX_JUDGE_NAME_CHARS:
        raise ValueError(
            f"judge name {name!r} has {len(name)} characters; the API allows {MAX_JUDGE_NAME_CHARS}"
        )
    if model_id is None:
        model_id = get_default_model_id(client)
    try:
        judge = client.judges.create(
            name=name, evaluation_goal=evaluation_goal, model_id=model_id
        )
        if created is not None:
            created.append(judge.id)
        return judge
    except Exception as exc:
        # Handle 409 Conflict (judge name already exists) by finding and returning the existing judge
        if "already exists" in str(exc) or "409" in str(exc):
            logger.info("Judge '%s' already exists, reusing.", name)
            existing = _find_judge(client, name)
            if existing is not None:
                if created is not None:
                    created.append(existing.id)
                return existing
        raise


def _find_judge(client: LayerLens, name: str) -> Any:
    """The project's judge named ``name``, or ``None``, reading every page of judges."""
    page = 1
    while True:
        resp = client.judges.get_many(page=page, page_size=100)
        judges = (resp.judges if resp else None) or []
        for j in judges:
            if j.name == name:
                return j
        if len(judges) < 100:
            return None
        page += 1


def poll_evaluation_results(
    client: LayerLens,
    evaluation_id: str,
    *,
    max_attempts: int = 60,
    initial_delay: float = 2.0,
    max_delay: float = 10.0,
    backoff_factor: float = 1.3,
) -> Optional[List[Any]]:
    """Poll for trace evaluation results with exponential backoff.

    Trace evaluations are **asynchronous**. When ``trace_evaluations.create()``
    returns, the evaluation has been accepted but execution has not yet started.
    The actual LLM judge execution takes a variable amount of time (typically
    5-60 seconds depending on model and trace complexity). During this window:

    - ``get_results()`` returns ``None`` (no results row written yet).
    - ``get_results()`` may return a result without a score (the row exists
      but execution is still in progress).

    Both cases are normal and expected. This helper retries with exponential
    backoff until a scored result appears, the evaluation fails, or the attempt
    budget is exhausted. A failed evaluation never gets results, so the helper
    stops polling as soon as its status is ``failure`` and logs the API's
    reason (``status_description``); an evaluation that no longer exists
    (``NotFoundError``) stops it too. A 429 or a 5xx is polled through.

    Args:
        client: An initialized :class:`LayerLens` client.
        evaluation_id: The trace evaluation ID to poll.
        max_attempts: Maximum number of poll attempts (default 60, ~3-4 min total).
        initial_delay: Initial delay in seconds between polls.
        max_delay: Maximum delay cap in seconds.
        backoff_factor: Multiplier applied to delay each iteration.

    Returns:
        A list of :class:`TraceEvaluationResult` objects, or ``None``
        if the evaluation failed or results were not available within the
        polling window.
    """
    delay = initial_delay
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.trace_evaluations.get_results(evaluation_id)
            if resp and resp.score is not None:
                return [resp]
            # None or missing score -- still running, or failed: read the status
            evaluation = client.trace_evaluations.get(evaluation_id)
            status = getattr(evaluation.status, "value", evaluation.status) if evaluation else None
            if status == "failure":
                logger.warning(
                    "Trace evaluation %s failed: %s",
                    evaluation_id,
                    getattr(evaluation, "status_description", None) or "no reason given",
                )
                return None
        except NotFoundError:
            # The evaluation itself is gone (deleted, or never in this project): it
            # will never have results, so stop polling. (No results yet is None, not this.)
            logger.warning("Trace evaluation %s no longer exists.", evaluation_id)
            return None
        except Exception:
            # A 429 or a 5xx: poll again.
            pass

        if attempt < max_attempts:
            time.sleep(delay)
            delay = min(delay * backoff_factor, max_delay)

    return None
