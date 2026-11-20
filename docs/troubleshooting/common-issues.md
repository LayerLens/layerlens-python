# Common Issues

This guide covers frequently encountered issues when using the LayerLens Python SDK and how to resolve them.

## Installation Issues

### Package Not Found

**Error**: `Could not find a version that satisfies the requirement layerlens`

Make sure to include the custom package index:

```bash
uv pip install --extra-index-url https://sdk.layerlens.ai/package layerlens
```

### Python Version Incompatibility

**Error**: `Requires-Python >=3.8`

The SDK requires Python 3.8 or higher, and some extras 3.9 or 3.10 (see [Python versions](../getting-started/installation.md#python-versions)). Check your version:

```bash
python --version
```

## Client Initialization

### Missing API Key

**Error**: `LayerLensError: The api_key client option must be set either by passing api_key to the client or by setting the LAYERLENS_API_KEY environment variable`

Set the environment variable or pass the key explicitly:

```bash
export LAYERLENS_API_KEY="your-api-key"
```

```python
client = LayerLens(api_key="your-api-key")
```

### Organization Not Found

**Error**: `AuthenticationError`, `PermissionDeniedError` (a bare 403 `Forbidden`) or `NotFoundError` from the first call, or from reading `client.organization_id`

Your API key may be invalid or expired, or `LAYERLENS_BASE_URL` may miss its `/api/v1` path. Building the client makes no request: the key's organization is read by the first call that needs it. To find out at start, read `client.organization_id` right after building the client. Verify the key in the [LayerLens dashboard](https://app.layerlens.ai).

## Evaluation Issues

### Evaluation Stuck in Progress

If `wait_for_completion` seems to hang, the evaluation may be queued behind other jobs. You can check the status manually:

```python
evaluation = client.evaluations.get_by_id(evaluation.id)
print(f"Status: {evaluation.status}")
```

### Model or Benchmark Not Found

When using `get_by_key`, ensure the key matches exactly (case-sensitive):

```python
# Correct
model = client.models.get_by_key("openai/gpt-4o")

# Search by name if unsure
models = client.models.get(type="public", name="gpt-4o")
```

## Trace Upload Issues

### File Too Large

Trace files must be under 50 MB. For larger datasets, split the file into smaller chunks.

### Invalid File Format

Trace uploads accept `.json` and `.jsonl` files only. Ensure your file contains valid JSON.

## Timeout Errors

**Error**: `APITimeoutError`

The default timeout is 10 minutes, with 5 seconds to connect. For long-running operations, increase it. The client's timeout applies to every call, and a call's own `timeout=` overrides it:

```python
# Every call of this client
client = LayerLens(timeout=1200.0)  # 20 minutes

# One call
result = client.evaluations.create(model=model, benchmark=benchmark, timeout=300.0)

# Every call of a copy of the client
result = client.with_options(timeout=300.0).evaluations.create(
    model=model,
    benchmark=benchmark,
)
```

One read has a limit of its own: the read of the key's organization before the first call (or when you read `client.organization_id`). Each wait within an attempt (to connect, and for each piece of the answer) is limited to 30 seconds, or the client's timeout when shorter, so a large client timeout doesn't lengthen it. See [Client Configuration](../api-reference/client.md#timeout-configuration).

## Network Issues

**Error**: `APIConnectionError`

- Verify your internet connection
- Check if `https://api.layerlens.ai` is reachable
- If behind a proxy, configure `httpx` accordingly

## See Also

- [Authentication Problems](authentication.md) for auth-specific issues
- [Error Codes Reference](error-codes.md) for the full exception hierarchy
