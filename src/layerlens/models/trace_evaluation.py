from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field, BaseModel, ConfigDict


class TraceEvaluationStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILURE = "failure"


class JudgeSnapshot(BaseModel):
    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())

    name: str
    version: int
    evaluation_goal: str = Field(..., alias="evaluationGoal")
    model_id: Optional[str] = Field(None, alias="modelId")
    model_name: Optional[str] = Field(None, alias="modelName")
    model_company: Optional[str] = Field(None, alias="modelCompany")


class TraceEvaluation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    trace_id: str
    judge_id: str
    status: TraceEvaluationStatus
    # Why the evaluation failed; set when status is "failure".
    status_description: Optional[str] = None
    # atlas-app sends this one key in camelCase, and the snapshot's own fields in snake_case
    # (JudgeSnapshot reads those by name).
    judge_snapshot: Optional[JudgeSnapshot] = Field(None, alias="judgeSnapshot")
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class TraceEvaluationStep(BaseModel):
    tool: str
    args: Dict[str, Any] = {}
    result: str = ""


class TraceEvaluationResult(BaseModel):
    id: str
    trace_evaluation_id: str
    trace_id: str
    judge_id: str
    score: float
    passed: bool
    reasoning: str
    steps: List[TraceEvaluationStep] = []
    model: str
    turns: int
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    total_cost: float
    created_at: str
