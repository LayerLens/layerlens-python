# LLM Provider Instrumentation

Provider adapters record every LLM call made inside a trace (a `@trace` function or a
`trace_context` block). No changes to your LLM calling code are needed.

## Supported Providers

| Provider | Adapter | Wraps |
| -------- | ------- | ----- |
| OpenAI | `instrument_openai(client)` | `client.chat.completions.create`, `client.responses.create` |
| Anthropic | `instrument_anthropic(client)` | `client.messages.create` |
| LiteLLM | `instrument_litellm()` | `litellm.completion`, `litellm.acompletion` |

LiteLLM provides a unified interface to 100+ providers (Azure, Google, Cohere, Mistral, Bedrock, etc.), so `instrument_litellm()` covers all of them.

## OpenAI

### Installation

```bash
uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[openai]"
```

### Usage

```python
import openai
from layerlens import LayerLens
from layerlens.instrument import trace
from layerlens.instrument.adapters.providers.openai import instrument_openai

client = LayerLens()
openai_client = openai.OpenAI()

# Instrument the client instance
instrument_openai(openai_client)

@trace(client)
def my_agent(question: str):
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": question}],
    )
    return response.choices[0].message.content

my_agent("What is Python?")
```

Each call records:

- **`model.invoke`** named `openai.chat.completions.create`: the `model`, the request `parameters`
  (`temperature`, `max_tokens`, ...), token `usage`, `latency_ms`, `finish_reason` and the response's
  id; with content capture, the `messages` and the reply
- **`tool.call`** for each tool the model asks to call: its name, and its arguments with content capture
- **`cost.record`**: the token counts and the call's `cost_usd`, when the response reports token usage
  (a Chat Completions stream does only with `stream_options={"include_usage": True}`; a Responses
  API stream's usage is not read yet)

A streamed call (`stream=True`) records these once its stream has been read to the end, so read it
inside the trace: a `@trace` function that returns the stream ends its trace first.

### Disconnect

`instrument_openai` returns the adapter; its `disconnect()` restores the client's original methods:

```python
from layerlens.instrument.adapters.providers.openai import instrument_openai, uninstrument_openai

provider = instrument_openai(openai_client)

# Later, restore original methods:
provider.disconnect()  # or uninstrument_openai()
```

## Anthropic

### Installation

```bash
uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[anthropic]"
```

### Usage

```python
import anthropic
from layerlens import LayerLens
from layerlens.instrument import trace
from layerlens.instrument.adapters.providers.anthropic import instrument_anthropic

client = LayerLens()
anthropic_client = anthropic.Anthropic()

instrument_anthropic(anthropic_client)

@trace(client)
def my_agent(question: str):
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

my_agent("What is Python?")
```

Each call records `model.invoke` named `anthropic.messages.create` (the `model`, the request
`parameters`, token `usage`, `stop_reason`, `latency_ms`, the count of each kind of content block;
with content capture, the messages and the reply), a `tool.call` for each tool use, and `cost.record`.

### Disconnect

```python
from layerlens.instrument.adapters.providers.anthropic import instrument_anthropic, uninstrument_anthropic

provider = instrument_anthropic(anthropic_client)
provider.disconnect()  # or uninstrument_anthropic()
```

## LiteLLM

LiteLLM works differently from OpenAI/Anthropic — it patches module-level functions rather than client instances.

### Installation

```bash
uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[litellm]"
```

### Usage

```python
import litellm
from layerlens import LayerLens
from layerlens.instrument import trace
from layerlens.instrument.adapters.providers.litellm import instrument_litellm

client = LayerLens()

# Patch litellm module (call once at startup)
instrument_litellm()

@trace(client)
def my_agent(question: str):
    response = litellm.completion(
        model="gpt-4o",
        messages=[{"role": "user", "content": question}],
    )
    return response.choices[0].message.content

my_agent("What is Python?")
```

Since LiteLLM supports 100+ providers, this single call instruments all of them:

```python
instrument_litellm()

@trace(client)
def multi_provider():
    # Each of these records model.invoke and cost.record
    litellm.completion(model="gpt-4o", messages=[...])
    litellm.completion(model="claude-sonnet-4-20250514", messages=[...])
    litellm.completion(model="gemini/gemini-pro", messages=[...])
```

### Uninstrument

```python
from layerlens.instrument.adapters.providers.litellm import uninstrument_litellm

uninstrument_litellm()
```

## Captured Parameters

All provider adapters record these request parameters, when present, in `model.invoke`'s `parameters`:

| Parameter | Description |
| --------- | ----------- |
| `temperature` | Sampling temperature |
| `max_tokens` | Maximum response tokens |
| `top_p` | Nucleus sampling parameter |
| `frequency_penalty` | Frequency penalty |
| `presence_penalty` | Presence penalty |
| `response_format` | Structured output format |

Response metadata varies by provider but always includes token usage when available.

## Passthrough Behavior

When called **outside** a trace, all adapters pass through to the original SDK method with zero overhead. This means you can instrument at startup and leave it on — it only records while a trace is running.

```python
instrument_openai(openai_client)

# No active trace — passes through directly to OpenAI
openai_client.chat.completions.create(model="gpt-4o", messages=[...])

@trace(client)
def traced_call():
    # Active trace — records model.invoke and cost.record
    openai_client.chat.completions.create(model="gpt-4o", messages=[...])
```

## Error Handling

If an LLM call raises inside a trace, the adapter records `agent.error` and re-raises the exception:

```python
@trace(client)
def my_agent():
    try:
        openai_client.chat.completions.create(model="gpt-4o", messages=[...])
    except openai.RateLimitError:
        pass  # agent.error: error_type="RateLimitError", error_code="rate_limit_exceeded"
```

`error_code` is the provider's own reason for the failure, read off the exception, so it is kept
when the error's text is not (the default records no content) and whatever the SDK's text says.

## Next Steps

- [Agent Frameworks](frameworks.md) — LangChain and LangGraph callback handlers
- [Quick Start](quickstart.md) — Manual instrumentation with `@trace`, `span()` and `emit()`
