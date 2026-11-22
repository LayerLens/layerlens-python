# Agent Framework Instrumentation

Framework adapters plug into agent frameworks as callback handlers. Unlike provider adapters (which
wrap SDK methods), framework adapters receive the framework's own callbacks and record each run as a
trace of its own.

## Supported Frameworks

| Framework | Adapter | Integration |
| --------- | ------- | ----------- |
| LangChain | `LangChainCallbackHandler` | Pass as a callback handler |
| LangGraph | `LangGraphCallbackHandler` | Pass as a callback handler |

## LangChain

### Installation

```bash
uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[langchain]"
```

### Usage

```python
from layerlens import LayerLens
from layerlens.instrument.adapters.frameworks.langchain import LangChainCallbackHandler

client = LayerLens()
handler = LangChainCallbackHandler(client)

# Pass the handler to any LangChain runnable
chain = prompt | llm | parser
result = chain.invoke(
    {"question": "What is RAG?"},
    config={"callbacks": [handler]},
)
```

The handler records:

| Callbacks | Events | Captured Data |
| --------- | ------ | ------------- |
| Chain start/end | `agent.input` / `agent.output` | Chain name; its input and output with content capture |
| LLM start/end | `model.invoke`, `cost.record`, `tool.call` | Model, parameters, token usage, latency, cost; each tool the model calls |
| Tool start/end | `tool.call` / `tool.result` | Tool name; its input and output with content capture |
| Retriever start/end | `tool.call` / `tool.result` | Retriever name; the query and documents with content capture |
| Any error | `agent.error` | `error_type`, the provider's `error_code`, `status="error"`; the text with content capture |

### How It Works

LangChain passes a `run_id` and a `parent_run_id` with every callback. The handler maps each run to
a span, so a nested run's events sit in a child span of its parent's:

1. An `on_chain_start` with no `parent_run_id` starts a run: it opens a trace with the run as its
   root span, or joins the trace of an enclosing `trace` or `trace_context` block
2. Each nested run (`on_llm_start`, `on_tool_start`, `on_retriever_start`, ...) gets a span under its parent
3. `on_*_end` and `on_*_error` record the run's result, or its error
4. When the root chain ends or fails, the trace it opened is sealed and uploaded. A trace it joined
   is uploaded by the block that opened it, when that block ends

An LLM, tool or retriever run with no chain around it, outside any `trace` or `trace_context`
block, starts no trace, and its events are not recorded.

### Example: RAG Chain

```python
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import StrOutputParser

from layerlens import LayerLens
from layerlens.instrument.adapters.frameworks.langchain import LangChainCallbackHandler

client = LayerLens()
handler = LangChainCallbackHandler(client)

prompt = ChatPromptTemplate.from_template("Answer: {question}")
llm = ChatOpenAI(model="gpt-4o")
chain = prompt | llm | StrOutputParser()

result = chain.invoke(
    {"question": "What is retrieval-augmented generation?"},
    config={"callbacks": [handler]},
)
```

This records a trace like:

```
RunnableSequence                    agent.input ... agent.output
├── ChatPromptTemplate              agent.input ... agent.output
├── ChatOpenAI                      model.invoke {model: "gpt-4o", usage: {total_tokens: 150}}, cost.record
└── StrOutputParser                 agent.input ... agent.output
```

### Error Handling

Chain, LLM, tool and retriever errors are recorded automatically:

```python
handler = LangChainCallbackHandler(client)

try:
    chain.invoke(input, config={"callbacks": [handler]})
except Exception:
    pass  # the trace still uploads, with the failure as agent.error
```

## LangGraph

The LangGraph adapter extends the LangChain handler with graph node awareness.

### Installation

```bash
uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[langchain]"
```

### Usage

```python
from layerlens import LayerLens
from layerlens.instrument.adapters.frameworks.langgraph import LangGraphCallbackHandler

client = LayerLens()
handler = LangGraphCallbackHandler(client)

# Use with a LangGraph compiled graph
result = graph.invoke(
    {"messages": [{"role": "user", "content": "Hello"}]},
    config={"callbacks": [handler]},
)
```

Besides the LangChain events, it records `agent.node.enter` and `agent.node.exit` for each graph
node it runs, and `agent.state.change` when a node changes the graph's state.

### Node Name Extraction

LangGraph attaches metadata to chain events that identifies which graph node is executing. The adapter extracts this to produce cleaner span names:

- Checks `metadata.langgraph_node` for the node name (highest priority)
- Falls back to the first plain tag (no colon), skipping internal `graph:step:*` tags
- Uses the chain name from `serialized` if neither is present

This means your traces show meaningful names like `agent`, `tools`, `retrieve` instead of generic `RunnableSequence` spans.

### Example Trace Output

```
StateGraph
├── agent      agent.node.enter, agent.node.exit
│   └── ChatOpenAI   model.invoke, cost.record
├── tools      agent.node.enter, agent.node.exit
│   └── search       tool.call, tool.result
└── agent      agent.node.enter, agent.node.exit
    └── ChatOpenAI   model.invoke, cost.record
```

## Framework vs Provider Adapters

You can use both together. For example, use the LangChain callback handler for the run structure, and a provider adapter to record each OpenAI call the chain makes:

```python
from layerlens.instrument.adapters.providers.openai import instrument_openai
from layerlens.instrument.adapters.frameworks.langchain import LangChainCallbackHandler

# Both can be active simultaneously
instrument_openai(openai_client)
handler = LangChainCallbackHandler(client)

chain.invoke(input, config={"callbacks": [handler]})
```

Note: When using both, a model call may be recorded twice (once by the provider adapter, once by the framework callback). In most cases, using just the framework adapter is sufficient since it records LLM calls through callbacks.

## Next Steps

- [LLM Providers](providers.md) — Auto-instrument OpenAI, Anthropic, and LiteLLM
- [Quick Start](quickstart.md) — Manual instrumentation with `@trace`, `span()` and `emit()`
