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
| `timeout`     | `float \| httpx.Timeout \| None` | No       | 10 minutes        | Request timeout configuration                                           |
| `max_retries` | `int`                            | No       | `2`               | Maximum number of retries on retryable errors (429, 500, 502, 503, 504) |

\*Required unless set via environment variables

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

### Simple Timeout

```python
from layerlens import LayerLens

# 30-second timeout for all requests
client = LayerLens(timeout=30.0)
```

### Retry Configuration

The client automatically retries requests that fail with retryable status codes (429 Too Many Requests, 500, 502, 503, 504) using exponential backoff from half a second. If the server sends a `Retry-After` header, as seconds or as an HTTP date, the client waits that long, up to 8 seconds; a header it cannot read leaves the wait to its own backoff.

The waits (`wait_for_completion`, and the environments `wait_until_ready`, `jobs.wait` and `runs.wait`) send each poll once and do their own retrying, so they keep going through a 429 for as long as their timeout allows, and honour a longer `Retry-After`, up to 5 minutes.

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

# Override timeout for a specific request
evaluation = client.with_options(timeout=120.0).evaluations.create(
    model=model,
    benchmark=benchmark
)
```
