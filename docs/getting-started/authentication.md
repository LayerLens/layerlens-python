# Authentication & Configuration

## API Key Setup

The SDK authenticates using an API key tied to your LayerLens organization. You can obtain your key from the [LayerLens dashboard](https://app.layerlens.ai).

### Environment Variable (Recommended)

Set the `LAYERLENS_API_KEY` environment variable and the client will pick it up automatically:

```bash
export LAYERLENS_API_KEY="your-api-key"
```

```python
from layerlens import LayerLens

# Automatically reads from LAYERLENS_API_KEY
client = LayerLens()
```

### Explicit API Key

Pass the key directly when constructing the client:

```python
from layerlens import LayerLens

client = LayerLens(api_key="your-api-key")
```

### Using a .env File

```bash
# .env (add this file to .gitignore)
LAYERLENS_API_KEY=your-api-key
```

```python
from dotenv import load_dotenv
load_dotenv()

from layerlens import LayerLens
client = LayerLens()
```

## Configuration Options

| Parameter  | Type                             | Default                           | Description     |
| ---------- | -------------------------------- | --------------------------------- | --------------- |
| `api_key`  | `str \| None`                    | `LAYERLENS_API_KEY` env           | Your API key    |
| `base_url` | `str \| httpx.URL \| None`       | `https://api.layerlens.ai/api/v1` | API base URL    |
| `timeout`  | `float \| httpx.Timeout \| None` | 10 minutes, 5 seconds to connect  | Timeout of every call; `None` is no timeout |

## Environment Variables

| Variable             | Description               | Default                           |
| -------------------- | ------------------------- | --------------------------------- |
| `LAYERLENS_API_KEY`  | Your API key              | (required)                        |
| `LAYERLENS_BASE_URL` | Override the API base URL | `https://api.layerlens.ai/api/v1` |

The variables from before the rename (`LAYERLENS_STRATIX_API_KEY`, `LAYERLENS_STRATIX_BASE_URL`) still work, with a deprecation warning. If both old and new are set, the new one wins, and the SDK warns when their values differ. See [Migrating from Stratix](../migrating-from-stratix.md).

## Organization & Project

The client finds your organization and project from your API key, and scopes every request to them. Building the client makes no request: the first call that needs them reads them, and raises if the key or URL is wrong (`AuthenticationError` or `PermissionDeniedError` for a wrong key, `APIConnectionError` when LayerLens is unreachable). A failed read is tried again on the next call.

To check the key and URL when your program starts, read `client.organization_id` right after building the client:

```python
from layerlens import LayerLens

client = LayerLens()
client.organization_id  # raises AuthenticationError for a wrong key
```

## Timeout Configuration

A call times out after 10 minutes by default, with 5 seconds to connect, and raises
`APITimeoutError`. The client's timeout applies to every call it makes, and a call's own
`timeout=` overrides it:

```python
# Every call of this client times out after 30 seconds
client = LayerLens(timeout=30.0)

# This call may take 2 minutes
evaluation = client.evaluations.create(model=model, benchmark=benchmark, timeout=120.0)

# A copy of the client whose every call may take 2 minutes; `client` keeps 30 seconds
evaluation = client.with_options(timeout=120.0).evaluations.create(
    model=model,
    benchmark=benchmark,
)
```

`timeout=None` is no timeout. The read of the key's organization before the first call has its own timeout: each wait within an attempt (to connect, and for each piece of the answer) is limited to 30 seconds, or the client's timeout when shorter. An attempt answered 429, 500, 502, 503 or 504 is retried, so the read can take longer; reading `organization_id` yourself on an `AsyncLayerLens` makes one attempt. See [Client Configuration](../api-reference/client.md#timeout-configuration).
