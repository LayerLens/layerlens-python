"""Map AG-UI event types to layerlens event names.

AG-UI defines 16 event types across five categories (lifecycle, text,
tool, state, special). The adapter delegates to ``map_agui_to_layerlens``
so new AG-UI event types only need a single line here to start flowing
through instrumentation.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from ....._deprecation import warn_deprecated


class AGUIEventType(str, Enum):
    """All known AG-UI event types."""

    # Lifecycle
    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_ERROR = "RUN_ERROR"
    # Text messages
    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
    # Tool calls
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
    TOOL_CALL_END = "TOOL_CALL_END"
    TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
    # State
    STATE_SNAPSHOT = "STATE_SNAPSHOT"
    STATE_DELTA = "STATE_DELTA"
    MESSAGES_SNAPSHOT = "MESSAGES_SNAPSHOT"
    # Special
    STEP_STARTED = "STEP_STARTED"
    STEP_FINISHED = "STEP_FINISHED"
    RAW = "RAW"


_AGUI_EVENT_MAP: dict[str, dict[str, str]] = {
    # Run lifecycle + state events map to SUPPRESSIBLE L6b stream types, not the
    # ALWAYS-ENABLED agent.state.change — so l6b_protocol_streams=False /
    # minimal() actually suppress them, and their raw event (carried under
    # data/payload) is stripped under capture_content=False (LAY-3578).
    "RUN_STARTED": {"layerlens_event": "protocol.stream.event", "category": "lifecycle"},
    "RUN_FINISHED": {"layerlens_event": "protocol.stream.event", "category": "lifecycle"},
    # RUN_ERROR is a run FAILURE, not ordinary lifecycle telemetry: route it to
    # agent.error (via build_run_error_payload) so the trace's derived status is
    # error, not silently completed — a generic protocol.stream.event is read by
    # no downstream engine (mirrors how a2a/mcp surface failures).
    "RUN_ERROR": {"layerlens_event": "agent.error", "category": "lifecycle"},
    "TEXT_MESSAGE_START": {
        "layerlens_event": "protocol.stream.event",
        "category": "text",
    },
    "TEXT_MESSAGE_CONTENT": {
        "layerlens_event": "protocol.stream.event",
        "category": "text",
    },
    "TEXT_MESSAGE_END": {"layerlens_event": "protocol.stream.event", "category": "text"},
    "TOOL_CALL_START": {"layerlens_event": "tool.call", "category": "tool"},
    "TOOL_CALL_ARGS": {"layerlens_event": "protocol.stream.event", "category": "tool"},
    "TOOL_CALL_END": {"layerlens_event": "protocol.stream.event", "category": "tool"},
    "TOOL_CALL_RESULT": {"layerlens_event": "tool.call", "category": "tool"},
    # agui.state matches the adapter's explicit STATE_* branches (reconciles the
    # wrap_stream vs middleware dual-path) and is L6b-suppressible with content
    # keys covering state/operations/payload/data.
    "STATE_SNAPSHOT": {"layerlens_event": "agui.state", "category": "state"},
    "STATE_DELTA": {"layerlens_event": "agui.state", "category": "state"},
    "MESSAGES_SNAPSHOT": {"layerlens_event": "agui.state", "category": "state"},
    "STEP_STARTED": {"layerlens_event": "protocol.stream.event", "category": "special"},
    "STEP_FINISHED": {"layerlens_event": "protocol.stream.event", "category": "special"},
    "RAW": {"layerlens_event": "protocol.stream.event", "category": "special"},
}


#: Fallback error_type for a RUN_ERROR that arrives without an explicit code.
AGUI_RUN_ERROR_TYPE = "agui_run_error"


def map_agui_to_layerlens(agui_event_type: str) -> dict[str, Any]:
    """Return the ``{layerlens_event, category}`` mapping for an AG-UI type."""
    return _AGUI_EVENT_MAP.get(
        agui_event_type,
        {"layerlens_event": "protocol.stream.event", "category": "unknown"},
    )


def map_agui_to_stratix(agui_event_type: str) -> dict[str, Any]:
    """Deprecated: use :func:`map_agui_to_layerlens`.

    Kept for callers of the pre-rename API (LAY-4220); returns the old
    ``{stratix_event, category}`` shape so existing key lookups keep working.
    """
    warn_deprecated(
        "layerlens.instrument.adapters.protocols.agui.map_agui_to_stratix",
        "map_agui_to_layerlens",
        stacklevel=2,
    )
    mapping = map_agui_to_layerlens(agui_event_type)
    return {"stratix_event": mapping["layerlens_event"], "category": mapping["category"]}


def build_run_error_payload(message: Any, code: Any) -> dict[str, Any]:
    """Canonical ``agent.error`` payload for an AG-UI ``RUN_ERROR`` event.

    Mirrors how the a2a / mcp adapters surface failures (``{error_type, status,
    error}`` + a ``source`` discriminator) so the trace's derived status is
    ``error`` and not silently ``completed``. ``code`` becomes ``error_type``
    when present; otherwise a generic honest type is used (never a fabricated
    one). ``message`` is carried verbatim as ``error`` when present.
    """
    payload: dict[str, Any] = {
        "source": "agui",
        "error_type": str(code) if code else AGUI_RUN_ERROR_TYPE,
        "status": "error",
    }
    if message is not None:
        payload["error"] = str(message)
    return payload


def get_all_agui_event_types() -> list[str]:
    return list(_AGUI_EVENT_MAP.keys())
