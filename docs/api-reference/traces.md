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
if result is not None:  # None for an answer the SDK could not read
    print(f"Uploaded {len(result.trace_ids)} traces")

# List traces
response = client.traces.get_many()
if response is not None:  # None for an answer the SDK could not read
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
    if result is not None:
        print(f"Uploaded {len(result.trace_ids)} traces")

    response = await client.traces.get_many()
    if response is not None:
        for trace in response.traces:
            print(f"  {trace.id}: {trace.filename}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Methods

Both the `LayerLens` (synchronous) and `AsyncLayerLens` (asynchronous) clients support the following methods.

Each method also takes `timeout=`: that call's timeout in place of the client's (seconds or an
`httpx.Timeout`; `None` is no timeout). Without it, the call uses the client's timeout: see
[Timeout Configuration](client.md#timeout-configuration).

### `upload(file_path)`

Uploads a JSON or JSONL trace file. This is a three-step process handled automatically:

1. Requests a presigned upload URL from the API
2. Uploads the file to S3 via the presigned URL, retried as the client retries its calls (a 429
   or a 5xx, or a refused or dropped connection, up to `max_retries` times; a timeout is not
   retried)
3. Creates trace records from the uploaded file

#### Parameters

| Parameter   | Type                             | Required | Description                             |
| ----------- | -------------------------------- | -------- | --------------------------------------- |
| `file_path` | `str`                            | Yes      | Path to the JSON or JSONL file          |
| `timeout`   | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `CreateTracesResponse` with the new traces' ids. An upload the API refuses raises its error, such as `BadRequestError` (400) for a file that holds no valid trace. An upload the storage refuses raises the SDK's status error too (`PermissionDeniedError` for a 403, `InternalServerError` once the retries of a 5xx run out); its message never quotes the presigned URL, whose query holds credentials.

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

### `get(id, strict=False)`

Retrieves a single trace by its unique identifier.

#### Parameters

| Parameter | Type                             | Required | Description              |
| --------- | -------------------------------- | -------- | ------------------------ |
| `id`      | `str`                            | Yes      | The unique trace ID      |
| `strict`  | `bool`                           | No       | Raise `LayerLensError`, not return `None`, for an answer that can't be read (default `False`) |
| `timeout` | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `Trace`. Raises `NotFoundError` if no trace has this id. Returns `None` if the API's answer is empty or can't be read as a trace; with `strict=True` that raises `LayerLensError` instead.

#### Example

```python
from layerlens import NotFoundError

try:
    trace = client.traces.get("trace-abc123")
except NotFoundError:
    print("No such trace")
else:
    if trace is None:  # the answer could not be read as a trace (strict=True raises instead)
        print("Could not read the trace")
    else:
        print(f"Trace: {trace.filename}")
        print(f"Data keys: {list(trace.data.keys())}")
```

### `get_many(strict=False, page=None, page_size=None, source=None, judge_id=None, status=None, time_range=None, search=None, tags=None, sort_by=None, sort_order=None)`

Retrieves multiple traces with filtering, searching, and pagination.

#### Parameters

| Parameter    | Type                             | Required | Description                                         |
| ------------ | -------------------------------- | -------- | --------------------------------------------------- |
| `strict`     | `bool`                           | No       | Raise `LayerLensError`, not return `None`, for an answer that can't be read (default `False`) |
| `page`       | `int \| None`                    | No       | Page number (1-based, defaults to 1)                |
| `page_size`  | `int \| None`                    | No       | Number of traces per page (default and maximum: 100) |
| `source`     | `str \| None`                    | No       | Filter by source (e.g., "upload")                   |
| `judge_id`   | `str \| None`                    | No       | Filter by associated judge                          |
| `status`     | `str \| None`                    | No       | Filter by evaluation status                         |
| `time_range` | `str \| None`                    | No       | Filter by time range (e.g., "7d", "30d")            |
| `search`     | `str \| None`                    | No       | Search term to filter traces                        |
| `tags`       | `Sequence[str] \| None`          | No       | Only traces that carry every one of these tags      |
| `sort_by`    | `str \| None`                    | No       | Field to sort by (e.g., "created_at")               |
| `sort_order` | `str \| None`                    | No       | Sort direction: "asc" or "desc"                     |
| `timeout`    | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `TracesResponse` object containing:

- `traces`: List of `TraceWithEvaluations` objects
- `count`: Number of traces in this page
- `total_count`: Total number of matching traces

A request the API refuses raises its error; see [Errors](errors.md). Returns `None` if the API's answer is empty or can't be read, a trace in it included; with `strict=True` that raises `LayerLensError` instead.

#### Example

```python
# Get all traces (None for an answer the SDK could not read; strict=True raises instead)
response = client.traces.get_many()
if response is not None:
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
if response is not None:
    for trace in response.traces:
        print(trace.id, trace.tags)
```

The traces of environment runs are listed only for an API key with Automation access: with a
Standard key, the example above returns none of them, and raises nothing.

`tags` is a list: one string raises `TypeError` rather than being read as its letters. An
empty tag raises `ValueError` before anything is sent, since the API would ignore it and
list every trace. `trace.tags` holds a trace's tags. The SDK's tracing tags the traces it records
with `trace(..., tags=[...])`, `trace_context(..., tags=[...])` and `LAYERLENS_TRACE_TAGS`: see
[Trace tags](../instrumentation/tags.md).

### `delete(id)`

Deletes a trace by its unique identifier.

#### Parameters

| Parameter | Type                             | Required | Description              |
| --------- | -------------------------------- | -------- | ------------------------ |
| `id`      | `str`                            | Yes      | The unique trace ID      |
| `timeout` | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns `True` once the trace is deleted. Raises `NotFoundError` if no trace has this id, for example when it was already deleted.

#### Example

```python
client.traces.delete("trace-abc123")
print("Trace deleted")
```

### `get_sources()`

Retrieves the list of available trace sources for the current project.

#### Parameters

| Parameter | Type                             | Required | Description              |
| --------- | -------------------------------- | -------- | ------------------------ |
| `timeout` | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

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
| `segments`        | `List[TraceSegment] \| None` | On `get`: every part of the trace, first part first. `None` when not known (see below) |
| `part`            | `int \| None`     | On `get`: this part's place among `segments`, from 1 |
| `parts`           | `int \| None`     | On `get`: how many parts the trace has. More than 1 for a trace continued in another process; `None` when not known |
| `segment_count`   | `int \| None`     | On `get_many`: how many parts the trace has, when it has more than one |

A trace continued in another process with `trace_context(from_context=...)` is stored as one
trace per process, its parts. `get_many` lists it once, through its first part, with
`segment_count`; read a later part by its id from `segments`:

```python
first = client.traces.get(trace_id)
for segment in (first.segments or []) if first is not None else []:
    if segment.id != first.id:
        later = client.traces.get(segment.id)
```

A trace the SDK recorded in one process is a trace of one part: `get` gives `segments` with
that part alone, and `part` and `parts` of `1`. `segments`, `part` and `parts` are `None` when
that isn't known: for a trace uploaded without a `segment_id` (by an SDK release before 2.0.0,
or a file you wrote), or read from an API older than layerlens-app#2888. `None` means unknown,
not "not continued". `(trace.parts or 0) > 1` is true for a trace known to be continued in
another process; when `parts` is `None`, it can't tell:

```python
trace = client.traces.get(trace_id)
# get returns None for an answer it can't read; parts is None when not known
if trace is not None and (trace.parts or 0) > 1:
    print(f"continued: part {trace.part} of {trace.parts}")
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
