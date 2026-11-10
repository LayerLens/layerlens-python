from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class TraceSegment(BaseModel):
    """One part of a trace continued in another process: what one process recorded of it.

    ``id`` is the part's own trace id, which ``traces.get`` reads. ``segment_id`` is the SDK
    collector's that uploaded it, and ``continued_from_span_id`` the span it picks up from in the
    process it was continued from (None for the first part).
    """

    id: str
    segment_id: str
    continued_from_span_id: Optional[str] = None
    start_time: Optional[str] = None
    event_count: Optional[int] = None
    attestation_status: Optional[str] = None


class Trace(BaseModel):
    id: str
    organization_id: str
    project_id: str
    created_at: str
    filename: str
    data: Dict[str, Any] = {}
    input: Optional[str] = None
    integration_id: Optional[str] = None
    tags: List[str] = []
    # The agent the API names for the trace, from what the trace declares; None when it names none.
    agent: Optional[str] = None
    # A trace continued in another process (``trace_context(from_context=...)``) is stored as one
    # trace per process, its parts. The read of a part names them all, first part first
    # (``segments``), its place among them (``part``, from 1) and their number (``parts``); a list
    # shows the trace once, through its first part, with ``segment_count`` when it has more than
    # one. None for a trace that is not continued, and from an API older than layerlens-app#2888.
    segments: Optional[List[TraceSegment]] = None
    part: Optional[int] = None
    parts: Optional[int] = None
    segment_count: Optional[int] = None


class TraceEvaluationSummary(BaseModel):
    judge_id: str
    judge_name: str
    judge_version: int
    created_at: str
    passed: Optional[bool] = None


class TraceWithEvaluations(Trace):
    evaluations_count: int = 0
    last_evaluations: List[TraceEvaluationSummary] = []
