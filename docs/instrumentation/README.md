# Instrumentation

The `layerlens.instrument` module records what an LLM application does as traces and uploads them
to LayerLens for evaluation. A trace is a list of events (the agent's input and output, each model
call, each tool call, any error), each in a span, sealed with a hash chain.

## How It Works

1. **`@trace(client)`** records one trace per call of a function: its input, its output or its
   error, and every event emitted while it runs. When the call ends, the trace is sealed and uploaded
   in the background.
2. **`span()`** groups the events emitted inside it under a child span. Spans nest through Python's
   `contextvars`, so they follow threads and `asyncio` tasks.
3. **`emit()`** records an event of your own, such as a retrieval or a tool call.
4. **Provider adapters** (OpenAI, Anthropic, LiteLLM, and others) wrap SDK methods, so every model
   call made in a trace records `model.invoke`, `cost.record` when the response reports token usage,
   and `agent.error` when it fails. A call made with `stream=True` records them once its stream has
   been read to the end, so read it inside the trace: a `@trace` function that returns the stream
   ends its trace before the caller reads it, and that trace has no `model.invoke` for the call.
   An OpenAI Chat Completions stream reports token usage only with
   `stream_options={"include_usage": True}`. An OpenAI Responses API stream's usage is not read
   yet: its `model.invoke` has no token counts, and it has no `cost.record`.
5. **Framework adapters** (LangChain, LangGraph, and others) plug into a framework's callbacks or
   hooks and record its runs as traces of their own.

## Quick Example

```python
from layerlens import LayerLens
from layerlens.instrument import emit, span, trace
from layerlens.instrument.adapters.providers.openai import instrument_openai

client = LayerLens()

# Instrument OpenAI: every chat.completions.create call made in a trace is recorded
import openai
openai_client = openai.OpenAI()
instrument_openai(openai_client)

@trace(client)
def my_agent(question: str):
    with span("retrieve"):
        docs = search(question)
        emit("retrieval.query", {"query": question, "result_count": len(docs)})

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": f"Context: {docs}"},
            {"role": "user", "content": question},
        ],
    )
    return response.choices[0].message.content

my_agent("What is retrieval-augmented generation?")
```

This records one trace:

```
my_agent (root span)
├── agent.input          the question
├── retrieve (span)
│   └── retrieval.query  the query and how many documents came back
├── model.invoke         gpt-4o: parameters, token usage, latency (recorded by the adapter)
├── cost.record          the call's cost in USD (when the response reports token usage)
└── agent.output         the answer
```

## Guides

- [Quick Start](quickstart.md) — `@trace`, `span()`, `emit()` and errors
- [LLM Providers](providers.md) — Auto-instrument OpenAI, Anthropic, and LiteLLM
- [Agent Frameworks](frameworks.md) — LangChain and LangGraph callback handlers
- [API Reference](../api-reference/instrumentation.md) — every function, its parameters, and uploading
- [Trace tags](tags.md) — Label traces with `tags=` or `LAYERLENS_TRACE_TAGS`, to find, grade and filter them

## Key Concepts

| Concept | Description |
| ------- | ----------- |
| **Trace** | Everything one run of your agent did, rooted at a `@trace` call, a `trace_context` block, or a framework adapter's run |
| **Event** | One thing that happened, with a type (`agent.input`, `model.invoke`, `tool.call`, `agent.error`, ...) and a payload |
| **Span** | A group of events within a trace; spans nest, and each event carries its span's id and its parent's |
| **Capture config** | What a trace records. `CaptureConfig.standard()`, the default, records no content (prompts, outputs, error text) |
| **Provider adapter** | Wraps an LLM SDK so each call records `model.invoke` and `cost.record` |
| **Framework adapter** | Turns a framework's callbacks or hooks into a trace's events |

## No-Op Safety

All instrumentation is no-op safe:

- Provider adapters pass through to the original SDK method when called outside a trace
- `emit()` records nothing outside a trace, and `span()` still runs its block
- Uploading happens on a background thread and never raises into your code
