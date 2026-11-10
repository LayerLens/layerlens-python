# Traces

The `traces` resource on the LayerLens client allows you to upload, retrieve, and manage trace data. Traces are flexible JSON documents representing execution data that can be evaluated by judges.

## Overview

Traces capture execution data (e.g., LLM interactions, agent workflows) in JSON or JSONL format. Once uploaded, traces can be evaluated using judges to assess quality and correctness.

### Using Synchronous Client

```python
from layerlens import LayerLens

client = LayerLens()

# Upload a trace file
result = client.traces.upload("./traces.jsonl")
print(f"Uploaded {len(result.trace_ids)} traces")

# List traces
response = client.traces.get_many()
for trace in response.traces:
    print(f"  {trace.id}: {trace.filename}")
```

### Using Async Client

```python
import asyncio
from layerlens import AsyncLayerLens

async def main():
    client = AsyncLayerLens()

    result = await client.traces.upload("./traces.jsonl")
    print(f"Uploaded {len(result.trace_ids)} traces")

    response = await client.traces.get_many()
    for trace in response.traces:
        print(f"  {trace.id}: {trace.filename}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Methods

Both the `LayerLens` (synchronous) and `AsyncLayerLens` (asynchronous) clients support the following methods.

### `upload(file_path, timeout=None)`

Uploads a JSON or JSONL trace file. This is a three-step process handled automatically:

1. Requests a presigned upload URL from the API
2. Uploads the file to S3 via the presigned URL
3. Creates trace records from the uploaded file

#### Parameters

| Parameter   | Type                             | Required | Description                             |
| ----------- | -------------------------------- | -------- | --------------------------------------- |
| `file_path` | `str`                            | Yes      | Path to the JSON or JSONL file          |
| `timeout`   | `float \| httpx.Timeout \| None` | No       | Override request timeout                |

#### Returns

Returns a `CreateTracesResponse` object if successful, `None` if the upload fails.

#### Constraints

- Maximum file size: 50 MB
- Supported formats: `.json`, `.jsonl`

#### Example

```python
# Upload a JSONL file
result = client.traces.upload("./my-traces.jsonl")
if result:
    print(f"Uploaded {len(result.trace_ids)} traces")

# Upload a JSON file
result = client.traces.upload("./single-trace.json")
```

### `get(id, timeout=None)`

Retrieves a single trace by its unique identifier.

#### Parameters

| Parameter | Type                             | Required | Description              |
| --------- | -------------------------------- | -------- | ------------------------ |
| `id`      | `str`                            | Yes      | The unique trace ID      |
| `timeout` | `float \| httpx.Timeout \| None` | No       | Override request timeout |

#### Returns

Returns a `Trace` object if found, `None` otherwise.

#### Example

```python
trace = client.traces.get("trace-abc123")
if trace:
    print(f"Trace: {trace.filename}")
    print(f"Data keys: {list(trace.data.keys())}")
```

### `get_many(page=None, page_size=None, source=None, judge_id=None, status=None, time_range=None, search=None, tags=None, sort_by=None, sort_order=None, timeout=None)`

Retrieves multiple traces with filtering, searching, and pagination.

#### Parameters

| Parameter    | Type                             | Required | Description                                         |
| ------------ | -------------------------------- | -------- | --------------------------------------------------- |
| `page`       | `int \| None`                    | No       | Page number (1-based, defaults to 1)                |
| `page_size`  | `int \| None`                    | No       | Number of traces per page (default: 100, max: 500)  |
| `source`     | `str \| None`                    | No       | Filter by source (e.g., "upload")                   |
| `judge_id`   | `str \| None`                    | No       | Filter by associated judge                          |
| `status`     | `str \| None`                    | No       | Filter by evaluation status                         |
| `time_range` | `str \| None`                    | No       | Filter by time range (e.g., "7d", "30d")            |
| `search`     | `str \| None`                    | No       | Search term to filter traces                        |
| `tags`       | `Sequence[str] \| None`          | No       | Only traces that carry every one of these tags      |
| `sort_by`    | `str \| None`                    | No       | Field to sort by (e.g., "created_at")               |
| `sort_order` | `str \| None`                    | No       | Sort direction: "asc" or "desc"                     |
| `timeout`    | `float \| httpx.Timeout \| None` | No       | Override request timeout                            |

#### Returns

Returns a `TracesResponse` object containing:

- `traces`: List of `TraceWithEvaluations` objects
- `count`: Number of traces in this page
- `total_count`: Total number of matching traces

Returns `None` if the request fails.

#### Example

```python
# Get all traces
response = client.traces.get_many()
print(f"Total: {response.total_count}")

# Filtered and sorted
response = client.traces.get_many(
    source="upload",
    time_range="7d",
    sort_by="created_at",
    sort_order="desc",
    page_size=20,
)

# The traces of the environment runs labelled 3f2c1ab (`--label`, here a commit SHA): a trace
# matches when it carries every tag
response = client.traces.get_many(tags=["environment", "support-desk", "3f2c1ab"])
for trace in response.traces:
    print(trace.id, trace.tags)
```

`tags` is a list: one string raises `TypeError` rather than being read as its letters. An
empty tag raises `ValueError` before anything is sent, since the API would ignore it and
list every trace. `trace.tags` holds a trace's tags. The SDK's tracing tags the traces it records
with `trace(..., tags=[...])`, `trace_context(..., tags=[...])` and `LAYERLENS_TRACE_TAGS`: see
[Trace tags](../instrumentation/tags.md).

### `delete(id, timeout=None)`

Deletes a trace by its unique identifier.

#### Parameters

| Parameter | Type                             | Required | Description              |
| --------- | -------------------------------- | -------- | ------------------------ |
| `id`      | `str`                            | Yes      | The unique trace ID      |
| `timeout` | `float \| httpx.Timeout \| None` | No       | Override request timeout |

#### Returns

Returns `True` if the trace was deleted, `False` otherwise.

#### Example

```python
deleted = client.traces.delete("trace-abc123")
if deleted:
    print("Trace deleted successfully")
```

### `get_sources(timeout=None)`

Retrieves the list of available trace sources for the current project.

#### Parameters

| Parameter | Type                             | Required | Description              |
| --------- | -------------------------------- | -------- | ------------------------ |
| `timeout` | `float \| httpx.Timeout \| None` | No       | Override request timeout |

#### Returns

Returns a `List[str]` of source names. Returns an empty list if no sources are found.

#### Example

```python
sources = client.traces.get_sources()
print(f"Available sources: {sources}")
```

## Response Objects

### Trace Object Properties

| Property          | Type              | Description                          |
| ----------------- | ----------------- | ------------------------------------ |
| `id`              | `str`             | Unique trace identifier              |
| `organization_id` | `str`             | Organization the trace belongs to    |
| `project_id`      | `str`             | Project the trace belongs to         |
| `created_at`      | `str`             | ISO 8601 creation timestamp          |
| `filename`        | `str \| None`     | Original filename of the upload      |
| `data`            | `Dict[str, Any]`  | The trace data                       |
| `input`           | `str \| None`     | Input text or prompt                 |
| `integration_id`  | `str \| None`     | Integration that created this trace  |
| `tags`            | `List[str]`       | The trace's tags; empty when it has none. `get_many(tags=...)` filters by them |
| `agent`           | `str \| None`     | The agent the API names for the trace, from what the trace declares; `None` when it names none |
| `segments`        | `List[TraceSegment] \| None` | On `get` of a trace continued in another process: every part of it, first part first. `None` for a trace that is not continued |
| `part`            | `int \| None`     | On `get` of a part: its place among `segments`, from 1 |
| `parts`           | `int \| None`     | On `get` of a part: how many parts its trace has |
| `segment_count`   | `int \| None`     | On `get_many`: how many parts the trace has, when it has more than one |

A trace continued in another process with `trace_context(from_context=...)` is stored as one
trace per process, its parts. `get_many` lists it once, through its first part, with
`segment_count`; read a later part by its id from `segments`:

```python
first = client.traces.get(trace_id)
for segment in first.segments or []:
    if segment.id != first.id:
        later = client.traces.get(segment.id)
```

### TraceSegment Object Properties

| Property                 | Type          | Description                                                        |
| ------------------------ | ------------- | ------------------------------------------------------------------ |
| `id`                     | `str`         | The part's trace id, which `get` reads                              |
| `segment_id`             | `str`         | The id of the SDK collector that recorded the part                  |
| `continued_from_span_id` | `str \| None` | The span the part picks up from in the process it continued; `None` for the first part |
| `start_time`             | `str \| None` | When the part's first event was recorded                            |
| `event_count`            | `int \| None` | How many events the part holds                                      |
| `attestation_status`     | `str \| None` | Whether the part's hash chain verified (`verified`, …)              |

### TraceWithEvaluations Object Properties

Extends `Trace` with:

| Property            | Type                           | Description                              |
| ------------------- | ------------------------------ | ---------------------------------------- |
| `evaluations_count` | `int \| None`                  | Number of evaluations run on this trace  |
| `last_evaluations`  | `List[TraceEvaluationSummary]` | Most recent evaluation summaries         |

### TraceEvaluationSummary Properties

| Property        | Type          | Description                        |
| --------------- | ------------- | ---------------------------------- |
| `judge_id`      | `str`         | ID of the judge used               |
| `judge_name`    | `str \| None` | Name of the judge                  |
| `judge_version` | `int \| None` | Version of the judge               |
| `created_at`    | `str \| None` | When the evaluation was run        |
| `passed`        | `bool \| None`| Whether the trace passed           |

## Next Steps

- Learn about [Judges](judges.md) to create evaluation criteria
- Learn about [Trace Evaluations](trace-evaluations.md) to run judges against traces
