# Quick Start Guide

This guide walks you through the most common SDK workflows.

## Setup

```bash
uv pip install --extra-index-url https://sdk.layerlens.ai/package layerlens
export LAYERLENS_API_KEY="your-api-key"
```

## Run a Benchmark Evaluation

```python
from layerlens import LayerLens

client = LayerLens()

# Get a model and benchmark by key
model = client.models.get_by_key("openai/gpt-4o")
benchmark = client.benchmarks.get_by_key("arc-agi-2")

# Create an evaluation
evaluation = client.evaluations.create(
    model=model,
    benchmark=benchmark,
)

# Wait for results
result = client.evaluations.wait_for_completion(evaluation)
print(f"Accuracy: {result.accuracy}")
```

## Create a Judge and Evaluate Traces

```python
from layerlens import LayerLens

client = LayerLens()

# Create a judge
judge = client.judges.create(
    name="Response Quality Judge",
    evaluation_goal="Rate whether the response is accurate, complete, and well-structured",
)

# Upload traces from a JSON/JSONL file
upload = client.traces.upload("./my_traces.json")
print(f"Uploaded {len(upload.trace_ids)} traces")

# Run a trace evaluation
trace_eval = client.trace_evaluations.create(
    trace_id=upload.trace_ids[0],
    judge_id=judge.id,
)

# Wait for completion and get results
result = client.trace_evaluations.wait_for_completion(trace_eval.id)
if result:
    print(f"Score: {result.score}, Passed: {result.passed}")
    print(f"Reasoning: {result.reasoning}")
```

## Async Usage

Every method is available in async form via `AsyncLayerLens`:

```python
import asyncio
from layerlens import AsyncLayerLens

async def main():
    client = AsyncLayerLens()

    model = await client.models.get_by_key("openai/gpt-4o")
    benchmark = await client.benchmarks.get_by_key("arc-agi-2")

    evaluation = await client.evaluations.create(
        model=model,
        benchmark=benchmark,
    )

    result = await client.evaluations.wait_for_completion(evaluation)
    print(f"Accuracy: {result.accuracy}")

asyncio.run(main())
```

## Browse Public Data

```python
from layerlens import LayerLens

client = LayerLens()

# List public models
models = client.public.models.get()
for model in models.models:
    print(f"{model.key}: {model.name}")

# List public benchmarks
benchmarks = client.public.benchmarks.get()
for bm in benchmarks.datasets:
    print(f"{bm.key}: {bm.name}")
```

## Error Handling

```python
from layerlens import LayerLens, NotFoundError, AuthenticationError, APIError

client = LayerLens()

try:
    model = client.models.get_by_id("nonexistent-id")
except NotFoundError as e:
    print(f"Not found: {e.message}")
except AuthenticationError as e:
    print(f"Auth failed: {e.message}")
except APIError as e:
    print(f"API error ({e.status_code}): {e.message}")
```
