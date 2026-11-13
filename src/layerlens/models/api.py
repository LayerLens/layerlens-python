from __future__ import annotations

from typing import Any, List, Optional

from pydantic import Field, BaseModel, ConfigDict, AliasChoices, model_validator

from .judge import Judge
from .model import Model
from .trace import TraceWithEvaluations
from .scorer import Scorer
from .benchmark import Benchmark
from .evaluation import Result, Evaluation
from .integration import Integration
from .organization import Organization
from .trace_evaluation import TraceEvaluation, TraceEvaluationResult
from .judge_optimization import JudgeOptimizationRun


class BenchmarksResponse(BaseModel):
    class Data(BaseModel):
        model_config = ConfigDict(populate_by_name=True)

        benchmarks: List[Benchmark] = Field(..., alias="datasets")

    data: Data


class CreateEvaluationsResponse(BaseModel):
    data: List[Evaluation]


class EvaluationsResponse(BaseModel):
    evaluations: List[Evaluation]
    pagination: Pagination


class ModelsResponse(BaseModel):
    class Data(BaseModel):
        models: List[Model]

    data: Data


class OrganizationResponse(BaseModel):
    data: Organization


class OrganizationsListResponse(BaseModel):
    data: List[Organization]


class ResultMetrics(BaseModel):
    total_count: int


class Pagination(BaseModel):
    page: int
    page_size: int
    total_pages: int
    total_count: int


class ResultsResponse(BaseModel):
    evaluation_id: str
    results: List[Result]
    metrics: ResultMetrics
    pagination: Pagination


class JudgesResponse(BaseModel):
    judges: List[Judge]
    count: int
    total_count: int


class CreateJudgeResponse(BaseModel):
    id: str


class UpdateJudgeResponse(BaseModel):
    organization_id: str
    project_id: str
    id: str


class DeleteJudgeResponse(BaseModel):
    organization_id: str
    project_id: str
    id: str


class TracesResponse(BaseModel):
    traces: List[TraceWithEvaluations]
    count: int
    total_count: int


class UploadURLResponse(BaseModel):
    organization_id: str
    project_id: str
    url: str


class CreateBenchmarkResponse(BaseModel):
    organization_id: str
    project_id: str
    benchmark_id: str


class CreateModelResponse(BaseModel):
    organization_id: str
    project_id: str
    model_id: str


class CreateTracesResponse(BaseModel):
    trace_ids: List[str]


class TraceEvaluationsResponse(BaseModel):
    trace_evaluations: List[TraceEvaluation]
    count: int
    total: int


class TraceEvaluationResultsResponse(TraceEvaluationResult):
    pass


class CostEstimateResponse(BaseModel):
    """What ``POST …/trace-evaluations/estimate`` answers.

    ``trace_count`` and ``cost_available`` always come. The price fields come only when the API
    could price the run: with ``cost_available`` False (a judge on a model the catalogue no longer
    carries) they are None, and the counts still say what the run would cover. The API sends no
    ``model``; the field stays, as None, for code that read it.
    """

    trace_count: int
    #: Whether the API could price the run. A reply without the field says so by its price.
    cost_available: bool
    estimated_cost: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    #: How many of the judges could not be priced; the estimate covers the others.
    unpriced_judges: Optional[int] = None
    #: How many traces the tags matched, for an estimate by tags.
    total_matched: Optional[int] = None
    model: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _cost_available_from_the_price(cls, data: Any) -> Any:
        if isinstance(data, dict) and "cost_available" not in data:
            return {**data, "cost_available": data.get("estimated_cost") is not None}
        return data


class JudgeOptimizationRunsResponse(BaseModel):
    optimization_runs: List[JudgeOptimizationRun]
    count: int
    total: int


class CreateJudgeOptimizationRunResponse(BaseModel):
    id: str
    judge_id: str
    budget: str
    status: str


class EstimateJudgeOptimizationCostResponse(BaseModel):
    estimated_cost: float
    annotation_count: int
    budget: str


class ApplyJudgeOptimizationResultResponse(BaseModel):
    judge_id: str
    new_version: int
    message: str


class IntegrationsResponse(BaseModel):
    integrations: List[Integration]
    count: int
    total_count: int


class TestIntegrationResponse(BaseModel):
    """``POST …/integrations/{id}/test``: whether the integration's stored credentials work.

    The API answers ``{"source_type": …, "ok": true}``; ``success`` reads ``ok``. Credentials the
    vendor refuses come back as an error (HTTP 401) carrying the vendor's reason, not as
    ``ok: false``.
    """

    success: bool = Field(validation_alias=AliasChoices("success", "ok"))
    message: Optional[str] = None


class ScorersResponse(BaseModel):
    scorers: List[Scorer]
    count: int
    total_count: int
