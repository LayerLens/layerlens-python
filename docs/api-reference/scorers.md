# Scorers

The `scorers` resource on the LayerLens client allows you to create and manage custom scorers for evaluating benchmark results. Scorers use an LLM model to evaluate model outputs using a custom prompt.

## Overview

A scorer defines a custom evaluation criterion backed by a specific LLM model and a prompt template. Custom scorers can be attached to custom benchmarks to provide additional scoring beyond the built-in metrics.

### Using Synchronous Client

```python
from layerlens import LayerLens

client = LayerLens()

# Fetch a model to use for the scorer
models = client.models.get(type="public", name="gpt-4o")
model = models[0]

# Create a scorer
scorer = client.scorers.create(
    name="Helpfulness Scorer",
    description="Evaluates how helpful the response is",
    model_id=model.id,
    prompt="Rate the helpfulness of the following response on a scale of 0 to 1.",
)

print(f"Created scorer: {scorer.name} (id={scorer.id})")

# List all scorers
response = client.scorers.get_many()
if response:
    for s in response.scorers:
        print(f"  {s.name}: {s.description}")
```

### Using Async Client

```python
import asyncio
from layerlens import AsyncLayerLens

async def main():
    client = AsyncLayerLens()

    scorer = await client.scorers.create(
        name="Helpfulness Scorer",
        description="Evaluates how helpful the response is",
        model_id="model-abc123",
        prompt="Rate the helpfulness of the following response on a scale of 0 to 1.",
    )

    print(f"Created scorer: {scorer.name}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Methods

Both the `LayerLens` (synchronous) and `AsyncLayerLens` (asynchronous) clients support the following methods.

Each method also takes `timeout=`: that call's timeout in place of the client's (seconds or an
`httpx.Timeout`; `None` is no timeout). Without it, the call uses the client's timeout: see
[Timeout Configuration](client.md#timeout-configuration).

### `create(name, description, model_id, prompt)`

Creates a new custom scorer.

#### Parameters

| Parameter     | Type                             | Required | Description                                        |
| ------------- | -------------------------------- | -------- | -------------------------------------------------- |
| `name`        | `str`                            | Yes      | Display name for the scorer                        |
| `description` | `str`                            | Yes      | Description of what the scorer evaluates           |
| `model_id`    | `str`                            | Yes      | ID of the LLM model to use for scoring             |
| `prompt`      | `str`                            | Yes      | Prompt template used to evaluate model outputs     |
| `timeout`     | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns the `Scorer` as stored: the API answers with the new scorer's id, and the scorer is read
back by it (if that read fails, the scorer is returned as sent, with its id). A create the API
refuses raises the API's error: `ConflictError` (409) for a name already taken in the project,
`BadRequestError` (400) for a field it refuses, such as a description under 10 characters.

### `get(id)`

Retrieves a scorer by its unique identifier.

#### Parameters

| Parameter | Type                             | Required | Description              |
| --------- | -------------------------------- | -------- | ------------------------ |
| `id`      | `str`                            | Yes      | The unique scorer ID     |
| `timeout` | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `Scorer`. Raises `NotFoundError` if no scorer has this id.

### `get_many(page=None, page_size=None)`

Retrieves multiple scorers with pagination.

#### Parameters

| Parameter   | Type                             | Required | Description                                          |
| ----------- | -------------------------------- | -------- | ---------------------------------------------------- |
| `page`      | `int \| None`                    | No       | Page number (1-based, defaults to 1)                 |
| `page_size` | `int \| None`                    | No       | Number of scorers per page (default and maximum: 100) |
| `timeout`   | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns a `ScorersResponse` object containing:

- `scorers`: List of `Scorer` objects
- `count`: Number of scorers in this page
- `total_count`: Total number of scorers

A request the API refuses raises its error; see [Errors](errors.md).

### `update(id, name=None, description=None, model_id=None, prompt=None)`

Updates an existing scorer. Only provided fields are modified; omitted fields remain unchanged.

#### Parameters

| Parameter     | Type                             | Required | Description                            |
| ------------- | -------------------------------- | -------- | -------------------------------------- |
| `id`          | `str`                            | Yes      | The unique scorer ID                   |
| `name`        | `str \| None`                    | No       | Updated display name                   |
| `description` | `str \| None`                    | No       | Updated description                    |
| `model_id`    | `str \| None`                    | No       | Updated model ID                       |
| `prompt`      | `str \| None`                    | No       | Updated prompt template                |
| `timeout`     | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns `True` if the update succeeded. It returns `False` for any failure, a missing id included, and does not raise.

### `delete(id)`

Deletes a scorer by its unique identifier.

#### Parameters

| Parameter | Type                             | Required | Description              |
| --------- | -------------------------------- | -------- | ------------------------ |
| `id`      | `str`                            | Yes      | The unique scorer ID     |
| `timeout` | `float \| httpx.Timeout \| None` | No       | This call's timeout instead of the client's; `None` is no timeout |

#### Returns

Returns `True` if the scorer was deleted. It returns `False` for any failure, a missing id included, and does not raise.

## Response Objects

### Scorer Object Properties

| Property          | Type          | Description                          |
| ----------------- | ------------- | ------------------------------------ |
| `id`              | `str`         | Unique scorer identifier             |
| `organization_id` | `str`         | Organization the scorer belongs to   |
| `project_id`      | `str`         | Project the scorer belongs to        |
| `name`            | `str`         | Display name                         |
| `description`     | `str \| None` | Description of what it evaluates     |
| `model_id`        | `str \| None` | ID of the backing LLM model         |
| `model_name`      | `str \| None` | Name of the backing LLM model       |
| `model_key`       | `str \| None` | Key of the backing LLM model        |
| `model_company`   | `str \| None` | Company that provides the model      |
| `prompt`          | `str \| None` | Prompt template for scoring          |
| `created_at`      | `str \| None` | ISO 8601 creation timestamp          |
| `updated_at`      | `str \| None` | ISO 8601 last update timestamp       |

## Next Steps

- Learn about [Benchmarks](models-benchmarks.md) to attach custom scorers to custom benchmarks
- Learn about [Judges](judges.md) for evaluating traces
