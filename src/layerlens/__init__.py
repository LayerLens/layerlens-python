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
from ._version import __version__ as __version__
from ._exceptions import (
    APIError,
    ConflictError,
    NotFoundError,
    APIStatusError,
    LayerLensError,
    RateLimitError,
    APITimeoutError,
    BadRequestError,
    APIConnectionError,
    AuthenticationError,
    InternalServerError,
    PaymentRequiredError,
    EnvironmentBuildError,
    PermissionDeniedError,
    EnvironmentConflictError,
    UnprocessableEntityError,
    APIResponseValidationError,
)
from ._deprecation import module_getattr
from ._public_client import PublicClient, AsyncPublicClient

__all__ = [
    "APIConnectionError",
    "APIError",
    "APIResponseValidationError",
    "APIStatusError",
    "APITimeoutError",
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
    "ConflictError",
    "EnvironmentBuildError",
    "EnvironmentConflictError",
    "Integration",
    "InternalServerError",
    "Judge",
    "JudgeOptimizationRun",
    "JudgeSnapshot",
    "JudgeVersion",
    "LayerLens",
    "LayerLensError",
    "NotFoundError",
    "OptimizationBudget",
    "OptimizationRunStatus",
    "PaymentRequiredError",
    "PermissionDeniedError",
    "PublicBenchmarkDetail",
    "PublicBenchmarksListResponse",
    "PublicClient",
    "PublicModelDetail",
    "PublicModelsListResponse",
    "RateLimitError",
    "Trace",
    "TraceEvaluation",
    "TraceEvaluationResult",
    "TraceEvaluationStatus",
    "TraceEvaluationStep",
    "TraceEvaluationSummary",
    "TraceWithEvaluations",
    "UnprocessableEntityError",
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
