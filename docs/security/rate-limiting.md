# Rate Limiting

This guide covers how to handle rate limiting when using the LayerLens Python SDK, including best practices for avoiding rate limits and properly handling rate limit errors.

## Identifying Rate Limit Errors

### Rate Limit HTTP Response

When you exceed rate limits, the API returns a `429 Too Many Requests` status:

```python
import layerlens
from layerlens import LayerLens

try:
    client = LayerLens()

    # Making many requests quickly (reads, so nothing is started or charged)
    for page in range(1, 101):
        client.evaluations.get_many(page=page, page_size=10)

except layerlens.RateLimitError as e:
    print(f"Rate limited: {e}")
    print(f"Status code: {e.status_code}")  # 429
    print(f"Retry-After: {e.response.headers.get('retry-after')}")
```

The client retries a 429 itself (twice by default, see
[Retry Configuration](../api-reference/client.md#retry-configuration)) before it raises
`RateLimitError`.
