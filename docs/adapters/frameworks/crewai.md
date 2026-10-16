# CrewAI adapter

Instruments [CrewAI](https://github.com/crewAIInc/crewAI) crews via CrewAI's
typed event bus (CrewAI ≥ 1.0). Earlier 0.x event-bus versions are also
handled via the dispatcher fallback.

## Install

```bash
uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[crewai]"
```

Pulls `crewai>=0.30.0`. CrewAI 0.30+ is Pydantic v2-only
(`requires_pydantic="2"` on the adapter).

## Usage

```python
from crewai import Agent, Crew, Task
from layerlens.instrument.adapters.frameworks import CrewAIAdapter

adapter = CrewAIAdapter(client=layerlens_client)
adapter.connect()                # registers handlers on CrewAI's event bus

crew = Crew(agents=[...], tasks=[...])
crew.kickoff()

adapter.disconnect()             # tears down handlers when done
```

## Event surface

- `agent.input` / `agent.output` per agent step and Crew task boundary.
- `tool.call` for every tool invocation, with the tool name + arguments.
- `tool.result` for each tool's output.
- `model.invoke` for the underlying LLM calls (provider-aware via the
  CrewAI agent's `llm` attribute).
- `cost.record` alongside `model.invoke` when token counts are reported.
- `agent.handoff` when CrewAI delegates between agents.
- `agent.error` for agent/tool failures.

## Traces

One trace per top-level execution: a `crew.kickoff()`, or a `Flow.kickoff()`
of your own. Whatever starts inside it is a nested span of that trace,
placed by CrewAI's event lineage: a crew run from a Flow method, an
`Agent.kickoff()`, and the internal `AgentExecutor` flow that CrewAI ≥ 1.14.7
runs each agent execution in. The trace is uploaded when the execution that
opened it finishes or fails. CrewAI reports a failed Flow from 1.15 on
(`FlowFailedEvent`); on earlier versions a failed Flow emits no end event, so
its trace is uploaded at `disconnect()`.

Thread-safety: CrewAI dispatches every handler on a thread pool, each in its
own copy of the emitting context, so the adapter keeps one run per top-level
execution and resolves each event to its run through that lineage. Concurrent
kickoffs through one adapter each get their own trace.

## Sample

[`samples/instrument/crewai/example.py`](../../../samples/instrument/crewai/example.py)

## Compat

- CrewAI 0.30+ (Pydantic v2-only)
- Python 3.9+
