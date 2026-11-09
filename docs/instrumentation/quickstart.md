# Instrumentation Quick Start

This guide covers the core instrumentation API: the `@trace` decorator, the `span()` context
manager, and `emit()`.

## Installation

The instrumentation module is included in the base SDK — no extra dependencies needed:

```bash
uv pip install --extra-index-url https://sdk.layerlens.ai/package layerlens
```

Provider adapters require their respective SDK as an optional dependency:

```bash
uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[openai]"      # OpenAI
uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[anthropic]"   # Anthropic
uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[litellm]"     # LiteLLM (100+ providers)
uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[langchain]"   # LangChain / LangGraph
```

## The `@trace` Decorator

`@trace(client)` records one trace per call of a function. It records the function's arguments as
`agent.input`, its return value as `agent.output` (or what it raised as `agent.error`), and every
event emitted while it runs. When the call ends, the trace is sealed and uploaded in the background.

### Using Synchronous Client

```python
from layerlens import LayerLens
from layerlens.instrument import trace

client = LayerLens()

@trace(client)
def my_agent(query: str):
    # Everything inside here is traced
    return process(query)

my_agent("Hello")
# → the trace is uploaded in the background
```

### Using Async Client

```python
import asyncio
from layerlens import AsyncLayerLens
from layerlens.instrument import trace

client = AsyncLayerLens()

@trace(client)
async def my_agent(query: str):
    return await process(query)

asyncio.run(my_agent("Hello"))
```

### Custom Names and Metadata

By default the root span is named after the function. Override it with `name`, and add keys to the
trace's `agent.input` event with `metadata`:

```python
@trace(client, name="qa-pipeline", metadata={"release": "2026.11"})
def run_pipeline(query: str):
    ...
```

### What Is Recorded

A trace records no content by default: `CaptureConfig.standard()` keeps the structure, models,
token counts, costs, latencies and error categories, and leaves out prompts, outputs and error
text. To record content too, pass a capture config:

```python
from layerlens.instrument import CaptureConfig

@trace(client, capture_config=CaptureConfig.full())
def my_agent(query: str):
    ...
```

## The `span()` Context Manager

Use `span()` inside a traced function to group the events of one step under a child span. It
yields the span's id; what the step did is the events emitted inside it, by you or by an adapter:

```python
from layerlens.instrument import emit, span, trace

@trace(client)
def my_agent(query: str):
    with span("retrieve"):
        docs = search(query)
        emit("retrieval.query", {"query": query, "result_count": len(docs)})

    with span("generate"):
        answer = call_llm(query, docs)  # an instrumented provider records model.invoke here

    return answer
```

### Nesting Spans

Spans nest automatically — the parent-child relationship is tracked via `contextvars`:

```python
@trace(client)
def my_agent(query: str):
    with span("outer"):
        with span("inner"):
            # inner is a child of outer
            ...
    with span("sibling"):
        # sibling is a child of root, not outer
        ...
```

This produces:

```
my_agent (root)
├── outer
│   └── inner
└── sibling
```

## Recording Your Own Events

`emit(event_type, payload)` records an event in the current span:

```python
from layerlens.instrument import emit

emit("tool.call", {"tool_name": "lookup_order", "input": {"order_id": "A-4471"}})
emit("agent.step", {"step": "plan", "count": 3})
```

Content keys (such as `input`, `output`, `query` and `error`) are dropped unless the trace's capture
config records content, and values shaped like API keys and tokens are scrubbed either way.

## Error Handling

Errors are captured automatically. If an exception escapes a traced function, the trace records it as
`agent.error` and still uploads; the exception propagates normally.

```python
@trace(client)
def my_agent(query: str):
    with span("risky-step"):
        raise ValueError("something broke")
    # → agent.error: error_type="ValueError", status="error"
    #   (and error="something broke" when the trace records content)
    # → the trace still uploads
    # → ValueError propagates to the caller
```

An error from an LLM provider also carries the provider's reason code as `error_code` (for example
`rate_limit_exceeded` or `invalid_api_key`), even when a framework wrapped it in an error of its own.
Like `error_type`, it is kept when content is not, so a trace always says why a call failed. See
[Error events](../api-reference/instrumentation.md#error-events).

## Next Steps

- [LLM Providers](providers.md) — Auto-instrument OpenAI, Anthropic, and LiteLLM
- [Agent Frameworks](frameworks.md) — LangChain and LangGraph callback handlers
