# Trace Evaluations

The `trace_evaluations` resource on the LayerLens client allows you to run judges against traces and retrieve scored results. This is how you assess the quality of your trace data using the evaluation criteria defined in your judges.

## Overview

A trace evaluation runs a specific judge against a specific trace, producing a scored result with reasoning and step-by-step analysis. You can estimate costs before running evaluations and retrieve detailed results afterwards.

### Using Synchronous Client

```python
from layerlens import LayerLens

client = LayerLens()

# Estimate cost before running
estimate = client.trace_evaluations.estimate_cost(
    trace_ids=["trace-1", "trace-2"],
    judge_id="judge-123",
)
if estimate and estimate.cost_available:
    print(f"Estimated cost: ${estimate.estimated_cost:.4f}")

# Run a judge on a trace
evaluation = client.trace_evaluations.create(
    trace_id="trace-1",
    judge_id="judge-123",
)
if evaluation is None:  # create returns None for an answer the SDK could not read
    raise SystemExit("No trace evaluation was created")

# Wait for completion and get results
result = client.trace_evaluations.wait_for_completion(evaluation.id)
if result:
    print(f"Score: {result.score}, Passed: {result.passed}")
    print(f"Reasoning: {result.reasoning}")
```

### Using Async Client

```python
import asyncio
from layerlens import AsyncLayerLens

async def main():
    client = AsyncLayerLens()

    evaluation = await client.trace_evaluations.create(
        trace_id="trace-1",
        judge_id="judge-123",
    )
    if evaluation is None:  # create returns None for an answer the SDK could not read
        return

    result = await client.trace_evaluations.wait_for_completion(evaluation.id)
    if result:
        print(f"Score: {result.score}, Passed: {result.passed}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Methods

Both the `LayerLens` (synchronous) and `AsyncLayerLens` (asynchronous) clients support the following methods.

Each method also takes `timeout=`: that call's timeout in place of the client's (seconds or an
`httpx.Timeout`; `None` is no timeout). Without it, the call uses the client's timeout: see
[Timeout Configuration](client.md#timeout-configuration).

### `create(trace_id, judge_id)`

Runs a judge against a trace, creating a new trace evaluation.

#### Parameters

| Parameter  | Type                             | Required | Description                    |
| ---------- | -------------------------------- | -------- | ------------------------------ |
| `trace_id` | `str`                            | Yes      | ID of the trace to evaluate    |
| `judge_id` | `str`                            | Yes      | ID of the judge to run         |
| `timeout`  | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns the new `TraceEvaluation`, or `None` if the API's answer can't be read as one. A create the API refuses raises its error: `NotFoundError` (404) for a judge or trace it can't find, `BadRequestError` (400) for an id that isn't one or a judge backed by a custom model.

#### Example

```python
evaluation = client.trace_evaluations.create(
    trace_id="trace-abc",
    judge_id="judge-xyz",
)
if evaluation is not None:  # None for an answer the SDK could not read
    print(f"Evaluation {evaluation.id}: {evaluation.status}")
```

### `get(id)`

Retrieves a trace evaluation by its unique identifier.

#### Parameters

| Parameter | Type                             | Required | Description                        |
| --------- | -------------------------------- | -------- | ---------------------------------- |
| `id`      | `str`                            | Yes      | The unique trace evaluation ID     |
| `timeout` | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `TraceEvaluation`, or `None` if the API's answer can't be read as one. Raises `NotFoundError` if no trace evaluation has this id.

### `get_many(page=None, page_size=None, judge_id=None, trace_id=None, outcome=None, time_range=None, search=None, sort_by=None, sort_order=None)`

Retrieves multiple trace evaluations with filtering and pagination.

#### Parameters

| Parameter    | Type                             | Required | Description                                                |
| ------------ | -------------------------------- | -------- | ---------------------------------------------------------- |
| `page`       | `int \| None`                    | No       | Page number (1-based, defaults to 1)                       |
| `page_size`  | `int \| None`                    | No       | Number of evaluations per page (default: 20, max: 100)     |
| `judge_id`   | `str \| None`                    | No       | Filter by judge                                            |
| `trace_id`   | `str \| None`                    | No       | Filter by trace                                            |
| `outcome`    | `str \| None`                    | No       | Filter by outcome (e.g., "pass", "fail")                   |
| `time_range` | `str \| None`                    | No       | Filter by time range (e.g., "7d", "30d")                   |
| `search`     | `str \| None`                    | No       | Search term to filter evaluations                          |
| `sort_by`    | `str \| None`                    | No       | Field to sort by (e.g., "created_at")                      |
| `sort_order` | `str \| None`                    | No       | Sort direction: "asc" or "desc"                            |
| `timeout`    | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `TraceEvaluationsResponse` object containing:

- `trace_evaluations`: List of `TraceEvaluation` objects
- `count`: Number of evaluations in this page
- `total`: Total number of matching evaluations

A request the API refuses raises its error; see [Errors](errors.md).

#### Example

```python
# Get all evaluations (None for an answer the SDK could not read)
response = client.trace_evaluations.get_many()
if response is not None:
    print(f"Total: {response.total}")

# Filtered by judge and outcome
response = client.trace_evaluations.get_many(
    judge_id="judge-123",
    outcome="pass",
    sort_by="created_at",
    sort_order="desc",
)
```

### `get_results(id)`

Retrieves the detailed results of a completed trace evaluation, including scores, reasoning, and step-by-step analysis.

Returns `None` while there are no results: the evaluation is pending, in progress or failed, or no trace evaluation has this id.

#### Parameters

| Parameter | Type                             | Required | Description                        |
| --------- | -------------------------------- | -------- | ---------------------------------- |
| `id`      | `str`                            | Yes      | The unique trace evaluation ID     |
| `timeout` | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `TraceEvaluationResultsResponse` object with the evaluation result fields (score, passed, reasoning, etc.).

Returns `None` if there are no results yet, or none at all (a failed evaluation, an unknown id). Any other refused request raises its error.

#### Example

```python
result = client.trace_evaluations.get_results("eval-123")
if result:
    print(f"Score: {result.score}")
    print(f"Passed: {result.passed}")
    print(f"Reasoning: {result.reasoning}")
    for step in result.steps:
        print(f"  Tool: {step.tool}, Result: {step.result}")
```

### `wait_for_completion(id, interval_seconds=3, timeout_seconds=300)`

Polls the evaluation status until it reaches a terminal state (success or failure), then returns the results. This is the recommended way to wait for trace evaluation results.

#### Parameters

| Parameter          | Type           | Required | Default | Description                                      |
| ------------------ | -------------- | -------- | ------- | ------------------------------------------------ |
| `id`               | `str`          | Yes      |         | The unique trace evaluation ID                   |
| `interval_seconds` | `int`          | No       | `3`     | Seconds between status polls                     |
| `timeout_seconds`  | `int \| None`  | No       | `300`   | Maximum wait time. `None` or `0` waits as long as it takes |

#### Returns

Returns a `TraceEvaluationResultsResponse` object if the evaluation completes successfully.

Returns `None` if the evaluation failed or no results are available. A trace evaluation that
no longer exists raises `NotFoundError` at once.

Raises `TimeoutError` if `timeout_seconds` is exceeded.

A status poll, or the read of the results, that fails on a 429, a 5xx or a lost
connection does not end the wait. Each request is sent once, and the wait does the
retrying: half a second later, then twice as long after each further failure, up to
`interval_seconds`. A wait never polls sooner than a `Retry-After` asks. When the `Retry-After`
reaches past the wait's deadline, the wait sleeps until the deadline and then raises, with the
429 as the cause. A wait with no deadline (`timeout_seconds=None`, or `0` on an evaluation
wait) reads a `Retry-After` longer than 5 minutes as 5 minutes. If those errors last until `timeout_seconds`, the
`TimeoutError` says the last poll failed and carries that error as its `__cause__`. Any
other error is raised at once.

`timeout_seconds` decides when the wait stops sending polls, not how long one answer may take
to arrive. Each poll's timeout is cut to the time left, but an HTTP timeout limits connecting
and each wait for data, not the whole answer: an answer that keeps arriving slowly can hold the
wait past `timeout_seconds`.

#### Example

```python
evaluation = client.trace_evaluations.create(
    trace_id="trace-abc",
    judge_id="judge-xyz",
)
if evaluation is None:  # create returns None for an answer the SDK could not read
    raise SystemExit("No trace evaluation was created")

# Wait up to 5 minutes for results
result = client.trace_evaluations.wait_for_completion(evaluation.id)
if result:
    print(f"Score: {result.score}, Passed: {result.passed}")
    print(f"Reasoning: {result.reasoning}")

# Custom timeout and polling interval
result = client.trace_evaluations.wait_for_completion(
    evaluation.id,
    interval_seconds=5,
    timeout_seconds=600,
)
```

### `estimate_cost(trace_ids, judge_id)`

Estimates the cost of running a judge against a set of traces before actually executing the evaluations.

#### Parameters

| Parameter   | Type                             | Required | Description                         |
| ----------- | -------------------------------- | -------- | ----------------------------------- |
| `trace_ids` | `List[str]`                      | Yes      | List of trace IDs to evaluate       |
| `judge_id`  | `str`                            | Yes      | ID of the judge to run              |
| `timeout`   | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `CostEstimateResponse`, or `None` when the API gives no answer. A request the API
refuses raises its error.

Check `estimate.cost_available` before you use the cost. When the API can't price the run (a
trace it can't find, a judge whose model has no price), `cost_available` is `False` and
`estimated_cost`, `input_tokens` and `output_tokens` are `None`. An unpriced estimate is falsy,
so `if estimate:` skips it too; use `if estimate is not None:` to read its `trace_count`.

#### Example

```python
estimate = client.trace_evaluations.estimate_cost(
    trace_ids=["trace-1", "trace-2", "trace-3"],
    judge_id="judge-123",
)
if estimate and estimate.cost_available:
    print(f"Estimated cost: ${estimate.estimated_cost:.4f}")
    print(f"Tokens: {estimate.input_tokens} in, {estimate.output_tokens} out")
if estimate is not None:  # an unpriced estimate is falsy, but has a trace_count
    print(f"Traces: {estimate.trace_count}")
```

## Response Objects

### TraceEvaluation Object Properties

| Property         | Type                         | Description                              |
| ---------------- | ---------------------------- | ---------------------------------------- |
| `id`             | `str`                        | Unique evaluation identifier             |
| `trace_id`       | `str`                        | ID of the evaluated trace                |
| `judge_id`       | `str`                        | ID of the judge used                     |
| `status`         | `TraceEvaluationStatus`      | Current status of the evaluation         |
| `status_description` | `str \| None`            | Why the evaluation failed, when `status` is `failure` |
| `judge_snapshot` | `JudgeSnapshot \| None`      | Snapshot of judge config at run time     |
| `created_at`     | `str \| None`                | ISO 8601 creation timestamp              |
| `started_at`     | `str \| None`                | When evaluation started                  |
| `finished_at`    | `str \| None`                | When evaluation finished                 |

#### TraceEvaluationStatus

| Status          | Description                          |
| --------------- | ------------------------------------ |
| `"pending"`     | Evaluation queued but not started    |
| `"in_progress"` | Evaluation currently running         |
| `"success"`     | Evaluation completed successfully    |
| `"failure"`     | Evaluation failed                    |

### TraceEvaluationResult Object Properties

| Property              | Type                          | Description                               |
| --------------------- | ----------------------------- | ----------------------------------------- |
| `id`                  | `str`                         | Unique result identifier                  |
| `trace_evaluation_id` | `str`                         | Parent evaluation ID                      |
| `trace_id`            | `str`                         | ID of the evaluated trace                 |
| `judge_id`            | `str`                         | ID of the judge used                      |
| `score`               | `float \| None`               | Numerical score                           |
| `passed`              | `bool \| None`                | Whether the trace passed the evaluation   |
| `reasoning`           | `str \| None`                 | Overall reasoning for the score           |
| `steps`               | `List[TraceEvaluationStep]`   | Step-by-step reasoning                    |
| `model`               | `str \| None`                 | Model used for evaluation                 |
| `turns`               | `int \| None`                 | Number of turns in evaluation             |
| `latency_ms`          | `int \| None`                 | Evaluation latency in milliseconds        |
| `prompt_tokens`       | `int \| None`                 | Number of prompt tokens used              |
| `completion_tokens`   | `int \| None`                 | Number of completion tokens used          |
| `total_cost`          | `float \| None`               | Total cost of the evaluation              |
| `created_at`          | `str \| None`                 | ISO 8601 creation timestamp               |

### CostEstimateResponse Properties

| Property          | Type            | Description                                                                 |
| ----------------- | --------------- | --------------------------------------------------------------------------- |
| `trace_count`     | `int`           | Number of traces the run would evaluate                                     |
| `cost_available`  | `bool`          | Whether the run could be priced; when `False` the four fields below are `None`, and the estimate is falsy |
| `estimated_cost`  | `float \| None` | Estimated cost in dollars                                                   |
| `input_tokens`    | `int \| None`   | Estimated input tokens                                                      |
| `output_tokens`   | `int \| None`   | Estimated output tokens                                                     |
| `unpriced_judges` | `int \| None`   | Judges that could not be priced; the estimate covers the others             |

## Next Steps

- Learn about [Judges](judges.md) to create evaluation criteria
- Learn about [Traces](traces.md) to upload data for evaluation
