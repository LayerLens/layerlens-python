# Client Configuration

The `LayerLens` (syncronous) and `AsyncLayerLens` (asyncronous) classes are the main entry points for interacting with the LayerLens sdk. This page covers client initialization, configuration options, and usage patterns.

## Basic Usage

### Syncronous Client

```python
from layerlens import LayerLens

# Construct syncronous client
# Loads for api key from the "LAYERLENS_API_KEY" enviornment variable
client = LayerLens()

# Explicit configuration
client = LayerLens(api_key="your_api_key")
```

### Asyncronous Client

```python
import asyncio
from layerlens import AsyncLayerLens

# Construct async client
# Loads for api key from the "LAYERLENS_API_KEY" enviornment variable
client = AsyncLayerLens()

# Explicit configuration
client = AsyncLayerLens(api_key="your_api_key")
```

## Constructor Parameters

### `LayerLens(api_key, base_url, timeout, max_retries)` and `AsyncLayerLens(api_key, base_url, timeout, max_retries)`

| Parameter     | Type                             | Required | Default           | Description                                                             |
| ------------- | -------------------------------- | -------- | ----------------- | ----------------------------------------------------------------------- |
| `api_key`     | `str \| None`                    | Yes\*    | `None`            | Your LayerLens API key                                                  |
| `base_url`    | `str \| httpx.URL \| None`       | No       | LayerLens API URL | Custom API base URL                                                     |
| `timeout`     | `float \| httpx.Timeout \| None` | No       | 10 minutes        | Timeout of every call the client makes; a call's own `timeout=` overrides it. `None` is no timeout. See [Timeout Configuration](#timeout-configuration) |
| `max_retries` | `int`                            | No       | `2`               | Maximum number of retries on retryable errors (429, 500, 502, 503, 504) |

\*Required unless set via environment variables

Constructing a client makes no request. The organization and project the key belongs to
(`client.organization_id`, `client.project_id`) are read from the API the first time a call
needs them, and that call raises what the API said: `APIConnectionError` when LayerLens is
unreachable, `AuthenticationError` for a wrong or revoked key. So a program that builds its
client at import, and traces with it, starts and runs while LayerLens is down; the traces it
cannot upload are counted as lost (see [Uploading](instrumentation.md#uploading)). A failed
read is tried again on the next call. To check the key when your program starts, read
`client.organization_id` there. Without any key, `LayerLens()` raises `LayerLensError` at once.

On `AsyncLayerLens`, the resource calls (`await client.traces.get_many()`, ...) read them
without blocking the event loop, through the client's own requests and retries, once for
concurrent first calls. Reading `client.organization_id` or `client.project_id` yourself before
any such call makes a blocking request; after one, the properties return what was read.

## Environment Variable Configuration

The client automatically loads configuration from these environment variables:

```bash
LAYERLENS_API_KEY="your_api_key_here"
```

## Public Client

For accessing public endpoints (models, benchmarks, comparisons), use `PublicClient` or `AsyncPublicClient`. See the [Public Client](public-client.md) reference for full details.

```python
from layerlens import PublicClient

# Loads API key from the "LAYERLENS_API_KEY" environment variable
public = PublicClient()
models = public.models.get(companies=["OpenAI"])
```

You can also access public endpoints from an authenticated client via the `.public` property:

```python
client = LayerLens()
public_models = client.public.models.get(query="claude")
```

## Timeout Configuration

A call times out after 10 minutes (600 s) by default, with 5 seconds to connect, and raises
`APITimeoutError`. The client's timeout applies to every call it makes: those of
`client.models`, `client.benchmarks`, `client.evaluations`, `client.traces`,
`client.environments` and every other resource, and of `client.public`. Each method also takes
its own `timeout=`, which overrides the client's for that call, except the methods of
`client.environments`, which take none: give those another timeout with a copy of the client,
`client.with_options(timeout=...)` (see below). A timeout is seconds (a float) or an
`httpx.Timeout`; `None` is no timeout.

The read of the key's organization and project, made before the first call that needs them (or
when you read `client.organization_id`), has a timeout of its own: each wait within an attempt
(to connect, to send, for a free connection, and for each piece of the answer) is limited to 30
seconds, or the client's timeout for that wait when shorter, whatever the client's timeout is,
`None` included. These limit each wait, not how long an attempt takes: an answer that keeps
arriving in pieces can take longer, and so can the whole read. An attempt answered 429, 500,
502, 503 or 504 is tried again, as any call is, up to `max_retries` times (2 by default), after
half a second, then twice as long each time, or after the reply's `Retry-After`, at most 8
seconds a wait. An attempt that times out or loses its connection is not tried again. On
`AsyncLayerLens`, reading `client.organization_id` yourself makes one attempt.

### Simple Timeout

```python
import httpx
from layerlens import LayerLens

# 30-second timeout for every call
client = LayerLens(timeout=30.0)

# 10 seconds to connect, 2 minutes for the rest
client = LayerLens(timeout=httpx.Timeout(120.0, connect=10.0))
```

### Retry Configuration

The client automatically retries requests that fail with retryable status codes (429 Too Many Requests, 500, 502, 503, 504) using exponential backoff from half a second. If the server sends a `Retry-After` header, as seconds or as an HTTP date, the client waits that long, up to 8 seconds; a header it cannot read leaves the wait to its own backoff.

The waits (`wait_for_completion`, and the environments `wait_until_ready`, `jobs.wait` and `runs.wait`) send each poll once and do their own retrying, so they keep going through a 429 for as long as their timeout allows. A wait never polls sooner than a `Retry-After` asks. When the `Retry-After` reaches past the wait's deadline, the wait sleeps until the deadline and then raises, with the 429 as the cause. A wait with no deadline (`timeout_seconds=None`, or `0` on an evaluation wait) reads a `Retry-After` longer than 5 minutes as 5 minutes.

```python
from layerlens import LayerLens

# Default: 2 retries
client = LayerLens()

# More retries for batch-heavy workloads
client = LayerLens(max_retries=5)

# Disable retries entirely
client = LayerLens(max_retries=0)
```

### Per-Request Timeout Override

```python
client = LayerLens()

# --- Models replace with the model name you want to run
models = client.models.get(type="public", name="gpt-4o")

if not models:
    print("gpt-4o not found on organization, exiting")

model = models[0]

# --- Benchmarks replace with the benchmark name you want to run
benchmarks = client.benchmarks.get(type="public", name="simpleQA")

if not benchmarks:
    print("SimpleQA benchmark not found on organization, exiting")

benchmark = benchmarks[0]

# Override the timeout for one call
evaluation = client.evaluations.create(model=model, benchmark=benchmark, timeout=120.0)

# Or use a copy of the client with another timeout for every call it makes
evaluation = client.with_options(timeout=120.0).evaluations.create(
    model=model,
    benchmark=benchmark
)
```

`with_options()` (also `copy()`) returns a new client with this client's options: its key, URL,
timeout, `max_retries`, bearer auth and upload hosts, and the organization and project it works
in. Pass `api_key`, `base_url`, `timeout`, `max_retries`, `use_bearer_auth` or
`trusted_upload_hosts` to change them; `timeout=None` is no timeout. The copy's timeout applies
to every call the copy makes, and the original client keeps its own. A copy with another key or
URL reads its own organization and project.
