# Judge Optimizations

The `judge_optimizations` resource on the LayerLens client allows you to optimize a judge's evaluation criteria using automated prompt engineering. This can improve the accuracy and consistency of your judge's evaluations.

## Overview

A judge optimization run takes an existing judge and refines its evaluation goal through automated testing and prompt engineering. You can estimate costs before running, monitor progress, and apply successful optimizations to update the judge.

### Using Synchronous Client

```python
from layerlens import LayerLens

client = LayerLens()

# Estimate cost before running
estimate = client.judge_optimizations.estimate(
    judge_id="judge-123",
    budget="medium",
)
print(f"Estimated cost: ${estimate.estimated_cost:.4f}")

# Start an optimization run
run = client.judge_optimizations.create(
    judge_id="judge-123",
    budget="medium",
)

# Poll for completion
import time
for _ in range(60):
    optimization = client.judge_optimizations.get(run.id)
    if optimization.status.value in ("success", "failure"):
        break
    time.sleep(5)

# Apply successful results
if optimization.status.value == "success":
    result = client.judge_optimizations.apply(run.id)
    print(f"New version: v{result.new_version}")
```

### Using Async Client

```python
import asyncio
from layerlens import AsyncLayerLens

async def main():
    client = AsyncLayerLens()

    run = await client.judge_optimizations.create(
        judge_id="judge-123",
        budget="medium",
    )

    # Poll for completion
    import time
    for _ in range(60):
        optimization = await client.judge_optimizations.get(run.id)
        if optimization.status.value in ("success", "failure"):
            break
        await asyncio.sleep(5)

    if optimization.status.value == "success":
        result = await client.judge_optimizations.apply(run.id)
        print(f"New version: v{result.new_version}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Methods

Both the `LayerLens` (synchronous) and `AsyncLayerLens` (asynchronous) clients support the following methods.

Each method also takes `timeout=`: that call's timeout in place of the client's (seconds or an
`httpx.Timeout`; `None` is no timeout). Without it, the call uses the client's timeout: see
[Timeout Configuration](client.md#timeout-configuration).

### `estimate(judge_id, budget="medium")`

Estimates the cost of running an optimization on a judge before actually executing it.

#### Parameters

| Parameter  | Type                             | Required | Description                                |
| ---------- | -------------------------------- | -------- | ------------------------------------------ |
| `judge_id` | `str`                            | Yes      | ID of the judge to optimize                |
| `budget`   | `str`                            | No       | Optimization budget: "light", "medium", or "heavy" (default: "medium") |
| `timeout`  | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns an `EstimateJudgeOptimizationCostResponse`. A request the API refuses raises its error, such as `BadRequestError` (400) for a `budget` other than `light`, `medium` or `heavy`.

#### Example

```python
estimate = client.judge_optimizations.estimate(
    judge_id="judge-123",
    budget="heavy",
)
print(f"Estimated cost: ${estimate.estimated_cost:.4f}")
print(f"Annotations: {estimate.annotation_count}")
print(f"Budget: {estimate.budget}")
```

### `create(judge_id, budget="medium")`

Starts a new optimization run for a judge. The optimization runs asynchronously on the server.

#### Parameters

| Parameter  | Type                             | Required | Description                                |
| ---------- | -------------------------------- | -------- | ------------------------------------------ |
| `judge_id` | `str`                            | Yes      | ID of the judge to optimize                |
| `budget`   | `str`                            | No       | Optimization budget: "light", "medium", or "heavy" (default: "medium") |
| `timeout`  | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `CreateJudgeOptimizationRunResponse`. A run the API refuses raises its error, such as `BadRequestError` (400) for a `budget` other than `light`, `medium` or `heavy`, or a judge with fewer than 10 annotations.

#### Example

```python
run = client.judge_optimizations.create(
    judge_id="judge-123",
    budget="medium",
)
print(f"Optimization {run.id}: {run.status}")
```

### `get(id)`

Retrieves the current state of an optimization run by its unique identifier.

#### Parameters

| Parameter | Type                             | Required | Description                        |
| --------- | -------------------------------- | -------- | ---------------------------------- |
| `id`      | `str`                            | Yes      | The unique optimization run ID     |
| `timeout` | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `JudgeOptimizationRun`. Raises `NotFoundError` if no optimization run has this id.

#### Example

```python
optimization = client.judge_optimizations.get("opt-run-123")
print(f"Status: {optimization.status}")
if optimization.status.value == "success":
    print(f"Baseline accuracy: {optimization.baseline_accuracy}")
    print(f"Optimized accuracy: {optimization.optimized_accuracy}")
```

### `get_many(judge_id=None, page=None, page_size=None)`

Retrieves multiple optimization runs with optional filtering and pagination.

#### Parameters

| Parameter   | Type                             | Required | Description                                            |
| ----------- | -------------------------------- | -------- | ------------------------------------------------------ |
| `judge_id`  | `str \| None`                    | No       | Filter by judge                                        |
| `page`      | `int \| None`                    | No       | Page number (1-based, defaults to 1)                   |
| `page_size` | `int \| None`                    | No       | Number of runs per page (default: 20, max: 100)        |
| `timeout`   | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `JudgeOptimizationRunsResponse` object containing:

- `optimization_runs`: List of `JudgeOptimizationRun` objects
- `count`: Number of runs in this page
- `total`: Total number of matching runs

A request the API refuses raises its error; see [Errors](errors.md).

#### Example

```python
# List all optimization runs
response = client.judge_optimizations.get_many()
print(f"Total runs: {response.total}")

# Filter by judge
response = client.judge_optimizations.get_many(judge_id="judge-123")
for run in response.optimization_runs:
    print(f"  {run.id}: {run.status} (budget: {run.budget})")
```

### `apply(id)`

Applies the results of a successful optimization run to the judge, updating its evaluation goal and creating a new judge version.

#### Parameters

| Parameter | Type                             | Required | Description                        |
| --------- | -------------------------------- | -------- | ---------------------------------- |
| `id`      | `str`                            | Yes      | The unique optimization run ID     |
| `timeout` | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns an `ApplyJudgeOptimizationResultResponse`. Raises `NotFoundError` if no optimization run has this id; a run the API can't apply raises its error.

#### Example

```python
result = client.judge_optimizations.apply("opt-run-123")
print(f"Applied to judge {result.judge_id}")
print(f"New version: v{result.new_version}")
print(f"{result.message}")
```

## Response Objects

### CreateJudgeOptimizationRunResponse Properties

| Property   | Type  | Description                        |
| ---------- | ----- | ---------------------------------- |
| `id`       | `str` | Unique optimization run identifier |
| `judge_id` | `str` | ID of the judge being optimized    |
| `budget`   | `str` | Optimization budget level          |
| `status`   | `str` | Initial status of the run          |

### JudgeOptimizationRun Properties

| Property             | Type                          | Description                                   |
| -------------------- | ----------------------------- | --------------------------------------------- |
| `id`                 | `str`                         | Unique optimization run identifier            |
| `judge_id`           | `str`                         | ID of the judge being optimized               |
| `status`             | `OptimizationRunStatus`       | Current status (pending, in_progress, success, failure) |
| `status_description` | `str \| None`                 | Human-readable status description             |
| `budget`             | `OptimizationBudget`          | Budget level (light, medium, heavy)           |
| `annotation_count`   | `int`                         | Number of annotations used                    |
| `baseline_accuracy`  | `float \| None`               | Accuracy before optimization                  |
| `optimized_accuracy` | `float \| None`               | Accuracy after optimization                   |
| `original_goal`      | `str \| None`                 | Original evaluation goal                      |
| `optimized_goal`     | `str \| None`                 | Optimized evaluation goal                     |
| `estimated_cost`     | `float`                       | Estimated cost in dollars                     |
| `actual_cost`        | `float`                       | Actual cost incurred                          |
| `created_at`         | `str`                         | ISO 8601 creation timestamp                   |
| `started_at`         | `str \| None`                 | When optimization started                     |
| `finished_at`        | `str \| None`                 | When optimization finished                    |
| `applied_at`         | `str \| None`                 | When results were applied to the judge        |
| `applied_version`    | `int \| None`                 | Judge version created by applying results     |

### EstimateJudgeOptimizationCostResponse Properties

| Property           | Type    | Description                        |
| ------------------ | ------- | ---------------------------------- |
| `estimated_cost`   | `float` | Estimated cost in dollars          |
| `annotation_count` | `int`   | Number of annotations to process   |
| `budget`           | `str`   | Budget level used for estimate     |

### ApplyJudgeOptimizationResultResponse Properties

| Property      | Type  | Description                                    |
| ------------- | ----- | ---------------------------------------------- |
| `judge_id`    | `str` | ID of the updated judge                        |
| `new_version` | `int` | New version number of the judge                |
| `message`     | `str` | Confirmation message                           |

## Optimization Budgets

| Budget     | Description                                                       |
| ---------- | ----------------------------------------------------------------- |
| `"light"`  | Faster, lower cost. Good for quick iterations.                    |
| `"medium"` | Balanced cost and thoroughness. Recommended default.              |
| `"heavy"`  | Most thorough optimization. Higher cost but potentially better results. |

## Next Steps

- Learn about [Judges](judges.md) to create judges for optimization
- Learn about [Trace Evaluations](trace-evaluations.md) to evaluate traces with optimized judges
