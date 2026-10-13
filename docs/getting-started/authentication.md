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
| `timeout`  | `float \| httpx.Timeout \| None` | 10 minutes                        | Request timeout |

## Environment Variables

| Variable             | Description               | Default                           |
| -------------------- | ------------------------- | --------------------------------- |
| `LAYERLENS_API_KEY`  | Your API key              | (required)                        |
| `LAYERLENS_BASE_URL` | Override the API base URL | `https://api.layerlens.ai/api/v1` |

The variables from before the rename (`LAYERLENS_STRATIX_API_KEY`, `LAYERLENS_STRATIX_BASE_URL`) still work, with a deprecation warning. If both old and new are set, the new one wins. See [Migrating from Stratix](../migrating-from-stratix.md).

## Organization & Project

On initialization, the client fetches your organization and project IDs automatically using your API key. These are used to scope all API requests.

## Timeout Configuration

```python
# Global timeout (30 seconds)
client = LayerLens(timeout=30.0)

# Per-request timeout override
evaluation = client.with_options(timeout=120.0).evaluations.create(
    model=model,
    benchmark=benchmark,
)
```
