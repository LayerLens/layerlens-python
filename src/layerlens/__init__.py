from typing import TYPE_CHECKING

from .models import (
    Judge,
    Trace,
    Integration,
    JudgeVersion,
    JudgeSnapshot,
    BenchmarkPrompt,
    TraceEvaluation,
    ComparisonResult,
    PublicModelDetail,
    ComparisonResponse,
    OptimizationBudget,
    TraceEvaluationStep,
    JudgeOptimizationRun,
    TraceWithEvaluations,
    OptimizationRunStatus,
    PublicBenchmarkDetail,
    TraceEvaluationResult,
    TraceEvaluationStatus,
    TraceEvaluationSummary,
    BenchmarkPromptsResponse,
    PublicModelsListResponse,
    PublicBenchmarksListResponse,
)
from ._client import Client, LayerLens, AsyncClient, AsyncLayerLens
from ._exceptions import (
    APIError,
    NotFoundError,
    LayerLensError,
    BadRequestError,
    AuthenticationError,
)
from ._deprecation import module_getattr
from ._public_client import PublicClient, AsyncPublicClient

__all__ = [
    "APIError",
    "AsyncClient",
    "AsyncLayerLens",
    "AsyncPublicClient",
    "AuthenticationError",
    "BadRequestError",
    "BenchmarkPrompt",
    "BenchmarkPromptsResponse",
    "Client",
    "ComparisonResult",
    "ComparisonResponse",
    "Integration",
    "Judge",
    "JudgeOptimizationRun",
    "JudgeSnapshot",
    "JudgeVersion",
    "LayerLens",
    "LayerLensError",
    "NotFoundError",
    "OptimizationBudget",
    "OptimizationRunStatus",
    "PublicBenchmarkDetail",
    "PublicBenchmarksListResponse",
    "PublicClient",
    "PublicModelDetail",
    "PublicModelsListResponse",
    "Trace",
    "TraceEvaluation",
    "TraceEvaluationResult",
    "TraceEvaluationStatus",
    "TraceEvaluationStep",
    "TraceEvaluationSummary",
    "TraceWithEvaluations",
]

# Deprecated names (LAY-4220): the same objects as their replacements.
if TYPE_CHECKING:
    Stratix = LayerLens
    AsyncStratix = AsyncLayerLens
    Atlas = LayerLens
    AsyncAtlas = AsyncLayerLens
    StratixError = LayerLensError
    AtlasError = LayerLensError
else:
    __getattr__ = module_getattr(
        __name__,
        {
            "Stratix": ("LayerLens", "layerlens.LayerLens"),
            "AsyncStratix": ("AsyncLayerLens", "layerlens.AsyncLayerLens"),
            "Atlas": ("LayerLens", "layerlens.LayerLens"),
            "AsyncAtlas": ("AsyncLayerLens", "layerlens.AsyncLayerLens"),
            "StratixError": ("LayerLensError", "layerlens.LayerLensError"),
            "AtlasError": ("LayerLensError", "layerlens.LayerLensError"),
        },
        globals(),
    )
