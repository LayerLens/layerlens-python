# Instrumentation

The `layerlens.instrument` module records what your agent does as a trace: a list of events (the
agent's input and output, each model call, each tool call, any error), grouped into spans, sealed
with a hash chain, and uploaded to LayerLens in the background. Provider and framework adapters
record their events for you.

## Overview

### Using Synchronous Client

```python
from layerlens import LayerLens
from layerlens.instrument import emit, span, trace

client = LayerLens()

@trace(client)
def my_agent(query: str) -> str:
    with span("retrieve"):
        docs = search(query)
        emit("retrieval.query", {"query": query, "result_count": len(docs)})
    return answer(query, docs)

my_agent("Hello")
```

### Using Async Client

```python
import asyncio
from layerlens import AsyncLayerLens
from layerlens.instrument import emit, span, trace

client = AsyncLayerLens()

@trace(client)
async def my_agent(query: str) -> str:
    with span("retrieve"):
        docs = await search(query)
        emit("retrieval.query", {"query": query, "result_count": len(docs)})
    return await answer(query, docs)

asyncio.run(my_agent("Hello"))
```

## Core API

### `trace(client, *, name=None, metadata=None, capture_config=None, tags=None)`

Decorator that records one trace per call of the function, and uploads it when the call ends.

#### Parameters

| Parameter        | Type                          | Required | Description                                                               |
| ---------------- | ----------------------------- | -------- | ------------------------------------------------------------------------- |
| `client`         | `LayerLens \| AsyncLayerLens` | Yes      | SDK client used to upload the trace                                       |
| `name`           | `str \| None`                 | No       | The root span's name (defaults to the function's name)                    |
| `metadata`       | `dict \| None`                | No       | Keys added to the trace's first event, `agent.input`                      |
| `capture_config` | `CaptureConfig \| None`       | No       | What the trace records (defaults to `CaptureConfig.standard()`, no content) |
| `tags`           | `list[str] \| None`           | No       | Tags for each trace, and for the traces started during the call. See [Trace tags](#trace-tags) |

#### Behavior

- Creates a `TraceCollector` for the call and makes it the current trace, with a root span named `name`
- Emits `agent.input`: the function's arguments as `input`, and the `metadata` keys
- Emits `agent.output` with the return value as `output` and `status="ok"`
- On an exception: emits `agent.error` (see [Error events](#error-events)) and re-raises it
- Then flushes the collector: seals the hash chain and queues the trace for upload (see [Uploading](#uploading))
- Works with both sync and async functions

#### Example

```python
@trace(client)
def my_agent(query: str):
    return process(query)

@trace(client, name="qa-pipeline", metadata={"release": "2026.11"})
async def my_async_agent(query: str):
    return await process(query)
```

### `span(name)`

Context manager that groups the events emitted inside it under a new span, a child of the current one.

#### Parameters

| Parameter | Type  | Required | Description              |
| --------- | ----- | -------- | ------------------------ |
| `name`    | `str` | Yes      | Display name of the span |

#### Returns

The new span's id, a `str`. Every event emitted inside the block carries it as `span_id`, with the
enclosing span's id as `parent_span_id`. The span itself holds no data: what happens in it is the
events you emit, and those adapters record.

#### Behavior

- Restores the enclosing span on exit, also when the block raises
- Records no event of its own: an exception raised in it is recorded once, as `agent.error`, by the
  `@trace` or adapter that sees it leave
- Outside a trace it still yields an id, and nothing emitted inside it is recorded

#### Example

```python
@trace(client)
def my_agent(query: str):
    with span("plan"):
        steps = plan(query)
        emit("agent.step", {"step": "plan", "count": len(steps)})
        with span("lookup"):  # a child of "plan"
            emit("tool.call", {"tool_name": "lookup", "input": steps[0]})
    with span("answer"):  # a child of the root span, not of "plan"
        return answer(steps)
```

### `emit(event_type, payload=None)`

Records one event into the current trace, in the current span. Does nothing outside a trace.

| Parameter    | Type           | Required | Description                                                    |
| ------------ | -------------- | -------- | -------------------------------------------------------------- |
| `event_type` | `str`          | Yes      | A registered event type, such as `tool.call`, `agent.step` or `retrieval.query` |
| `payload`    | `dict \| None` | No       | The event's fields                                              |

Every payload passes through the trace's `CaptureConfig` (content such as `input`, `output`, `query`
and `error` is stripped when `capture_content=False`, the default) and a secret scrub (values shaped
like API keys and tokens are replaced) before it is recorded.

### `trace_context(client, *, capture_config=None, from_context=None, tags=None)`

Context manager that opens one trace for everything inside it: several adapters, or your own
`emit` calls, record into the same trace instead of one trace each. Yields the `TraceCollector`,
and uploads the trace when the block exits, or, when a provider stream opened in the block is
still open, once that stream ends (see [LLM provider instrumentation](../instrumentation/providers.md)).

| Parameter        | Type                    | Required | Description                                                  |
| ---------------- | ----------------------- | -------- | ------------------------------------------------------------ |
| `client`         | `LayerLens \| AsyncLayerLens` | Yes | SDK client used to upload the trace                          |
| `capture_config` | `CaptureConfig \| None` | No       | What the trace records (defaults to `CaptureConfig.standard()`) |
| `from_context`   | `dict \| None`          | No       | What `get_trace_context()` returned on the other side of a boundary: the trace continues under its `trace_id` |
| `tags`           | `list[str] \| None`     | No       | Tags for this trace, and for the traces started inside the block. See [Trace tags](#trace-tags) |

```python
from layerlens.instrument import trace_context
from layerlens.instrument.adapters.providers.openai import instrument_openai

instrument_openai(openai_client)

with trace_context(client) as collector:
    plan = openai_client.chat.completions.create(model="gpt-4o-mini", messages=messages)
    run_tools(plan)
print(collector.trace_id)
```

### `get_trace_context()`

The current trace as a plain `dict` (`trace_id`, `span_id`, `parent_span_id`, `version`), to carry
it across a process or service boundary and continue it there with
`trace_context(client, from_context=...)`. `None` outside a trace. `inject_headers()` and
`extract_headers()` carry it as a W3C `traceparent` header instead.

Each process uploads its own part of a continued trace: every `TraceCollector` sends a
`segment_id` of its own beside the shared `trace_id`, and the one that continues a trace also
sends `continued_from` with the span it continues (a span id as `get_trace_context()` gives it;
any other value is left out). LayerLens keeps one trace per process's part, so neither side
replaces the other, however they are uploaded, and each part's attestation verifies on its own.
They share the `trace_id`; until the trace views group them, they list as separate traces.

### `TraceCollector`

Collects one trace's events. `@trace` makes one per call; `trace_context` yields its own.

| Member                                                                 | Description                                                         |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------- |
| `trace_id`                                                             | The trace's id                                                      |
| `segment_id`                                                           | This collector's part of the trace, the same for every upload of it |
| `emit(event_type, payload, span_id, parent_span_id=None, span_name=None)` | Records an event in a span you name, rather than the current one |
| `flush()`                                                              | Seals the trace and queues it for upload; later `emit` calls do nothing |
| `sealed`                                                               | `True` once flushed                                                 |

### Error events

An exception recorded by `@trace` or by an adapter becomes an `agent.error` event:

| Field        | Kept when `capture_content=False` | Description                                                                 |
| ------------ | --------------------------------- | --------------------------------------------------------------------------- |
| `error`      | No: it is content                 | The exception's text, with secrets scrubbed                                 |
| `error_type` | Yes                               | The exception's class name, such as `RateLimitError`                        |
| `error_code` | Yes                               | The provider's own reason, when the exception or one that caused it carries it |
| `status`     | Yes                               | `"error"`                                                                    |

`error_code` holds the provider's machine-readable reason whatever the error's text says: SDKs
reword their errors (openai's text left out the reason code from 3.38), and a framework usually
wraps the provider's exception in its own. It is read off the exception and the chain of errors
that caused it, nearest first:

| Provider SDK                                    | Where the reason is            | Examples                                          |
| ----------------------------------------------- | ------------------------------ | ------------------------------------------------- |
| openai, Azure OpenAI, LiteLLM                   | `exc.code`                     | `rate_limit_exceeded`, `invalid_api_key`, `model_not_found` |
| A relayed error body (groq, pydantic-ai)        | `exc.body["error"]["code"]`    | `rate_limit_exceeded`                             |
| AWS (`botocore` `ClientError`)                  | `exc.response["Error"]["Code"]` | `ThrottlingException`, `ValidationException`     |
| Google Gen AI                                   | `exc.status`                   | `RESOURCE_EXHAUSTED`                              |
| anthropic                                       | `exc.type`                     | `rate_limit_error`, `overloaded_error`            |

Only a short token is kept (letters, digits and `_ . : / -`, up to 64 characters): a value with
spaces, or a number such as an HTTP status, is not a reason code and is left out.

### Uploading

Each client gets its own upload queue and background thread, so recording never waits on the
network:

- The queue holds 64 traces. A trace that arrives when it is full is dropped, with a warning.
- After 10 failed uploads in a row, uploads stop for 60 seconds (a circuit breaker), and the
  traces that come meanwhile are dropped.
- When the process exits, the SDK sends what is still queued, waiting at most
  `LAYERLENS_EXIT_TIMEOUT` seconds in all (default 5), however many clients it has, and no
  longer than it takes. A trace still unsent then is lost with the process: it is counted
  (`exit_timeout`) and a warning says how many. A process that is killed loses what is queued
  without a word.

An upload takes a second or two, so a job that records many traces and then exits loses those
the default 5 seconds cannot send. A job that needs every trace sets `LAYERLENS_EXIT_TIMEOUT`
(for example `120`; read when the process exits, `0` waits for nothing), or calls `flush()`
where it needs them sent:

```python
from layerlens.instrument import flush

if not flush(timeout=60):
    print("some traces were still uploading after 60 s")
```

`flush(timeout=None)` waits until every client's queue and the upload in flight are done, or
`timeout` seconds pass (`None` waits as long as it takes), and returns whether nothing is left
to send. What is left when it returns `False` goes on uploading. A trace is queued when its
`trace` or `trace_context` block ends, so a block still open is not waited for. A block that
handed out a provider stream it opened is queued once that stream ends (read, closed or dropped),
or at exit. The default
stays short on purpose: an interactive program or a service is not held at exit when
LayerLens is slow or down.

`get_upload_loss_stats()` returns the number of traces dropped, by reason (`queue_full`,
`circuit_open`, `serialize_error`, `rejected`, `upload_error`, `exit_timeout`), and
`set_upload_loss_callback(callback)` calls `callback(reason, trace)` each time one is dropped.

## Trace tags

`trace(client, tags=[...])`, `trace_context(client, tags=[...])` and
`TraceCollector(client, config, tags=[...])` tag the traces they record, and the traces started
inside them. `LAYERLENS_TRACE_TAGS` (comma-separated, or a JSON array of strings) tags every trace
the process records. A trace is uploaded with the variable's tags, then those of the blocks it
started in, then its own, each trimmed and secret-scrubbed, without blanks or repeats, at most
32 of at most 128 bytes. `TraceCollector.tags` returns them. Tags are labels, not evidence:
they sit outside the attestation chain and are sent whatever the capture configuration. See the
[Trace tags guide](../instrumentation/tags.md).

## Provider Adapters

### `instrument_openai(client)`

Wraps `client.chat.completions.create` (and the Responses API) on an OpenAI client instance, so
every call made in a trace records `model.invoke` and `cost.record`, and `agent.error` when it fails.

```python
from layerlens.instrument.adapters.providers.openai import instrument_openai

instrument_openai(openai_client)
```

#### Classes

| Class            | Description                                                        |
| ---------------- | ------------------------------------------------------------------ |
| `OpenAIProvider` | What `instrument_openai` returns; `disconnect()` restores the client |

### `instrument_anthropic(client)`

Wraps `client.messages.create` on an Anthropic client instance.

```python
from layerlens.instrument.adapters.providers.anthropic import instrument_anthropic

instrument_anthropic(anthropic_client)
```

#### Classes

| Class               | Description                                                           |
| ------------------- | --------------------------------------------------------------------- |
| `AnthropicProvider` | What `instrument_anthropic` returns; `disconnect()` restores the client |

### `instrument_litellm()`

Wraps `litellm.completion` and `litellm.acompletion` at the module level.

```python
from layerlens.instrument.adapters.providers.litellm import instrument_litellm, uninstrument_litellm

instrument_litellm()      # Patch
uninstrument_litellm()    # Restore
```

## Framework Adapters

### `LangChainCallbackHandler(client, capture_config=None)`

A LangChain `BaseCallbackHandler` that records a chain run as a trace.

```python
from layerlens.instrument.adapters.frameworks.langchain import LangChainCallbackHandler

handler = LangChainCallbackHandler(client)
chain.invoke(input, config={"callbacks": [handler]})
```

#### Supported Callbacks

| Callback                                                          | Events                                        |
| ----------------------------------------------------------------- | --------------------------------------------- |
| `on_chain_start` / `on_chain_end`                                 | `agent.input` / `agent.output`                |
| `on_llm_start`, `on_chat_model_start` / `on_llm_end`              | `model.invoke`, `cost.record`, and `tool.call` for each tool the model calls |
| `on_tool_start` / `on_tool_end`                                   | `tool.call` / `tool.result`                   |
| `on_retriever_start` / `on_retriever_end`                         | `tool.call` / `tool.result`                   |
| `on_agent_action` / `on_agent_finish`                             | `agent.input` / `agent.output`                |
| `on_chain_error`, `on_llm_error`, `on_tool_error`, `on_retriever_error` | `agent.error`                           |

### `LangGraphCallbackHandler(client, capture_config=None)`

Extends `LangChainCallbackHandler` with LangGraph node names and state changes.

```python
from layerlens.instrument.adapters.frameworks.langgraph import LangGraphCallbackHandler

handler = LangGraphCallbackHandler(client)
graph.invoke(input, config={"callbacks": [handler]})
```

Extracts node names from `metadata.langgraph_node` or plain tags (skipping internal `graph:step:*` tags).

## Next Steps

- [Instrumentation Guide](../instrumentation/README.md) for usage patterns and examples
- [Traces API Reference](traces.md) for the underlying upload mechanism
