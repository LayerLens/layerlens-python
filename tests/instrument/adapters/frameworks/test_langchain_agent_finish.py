"""An agent's finish does not end the trace before its chain does.

LangChain reports ``on_agent_finish`` with the run id of the chain that runs the agent, the root run
for a top-level ``AgentExecutor``. The handler ended the trace there, so the root chain's own end,
which follows, emitted its ``agent.output`` into no trace: the trace kept the finish's output and
lost the chain's (docs E2E 2026-11-10, L7 AD-04). Only the root chain's end (or error) ends it now.
"""

from __future__ import annotations

from uuid import uuid4
from typing import Any

from langchain_core.agents import AgentAction, AgentFinish

from layerlens.instrument._capture_config import CaptureConfig
from layerlens.instrument.adapters.frameworks.langchain import LangChainCallbackHandler

from .conftest import find_events, capture_framework_trace


def test_the_root_chains_output_follows_the_agents_finish(mock_client: Any) -> None:
    uploaded = capture_framework_trace(mock_client)
    handler = LangChainCallbackHandler(mock_client, capture_config=CaptureConfig(capture_content=True))
    root = uuid4()

    handler.on_chain_start({"name": "AgentExecutor"}, {"input": "weather in Paris?"}, run_id=root, name="weather_agent")
    handler.on_agent_action(AgentAction(tool="get_weather", tool_input="Paris", log="look it up"), run_id=root)
    handler.on_agent_finish(AgentFinish(return_values={"output": "sunny"}, log="done"), run_id=root)
    assert mock_client.traces.upload.call_count == 0, "the trace ended at the agent's finish"
    handler.on_chain_end({"output": "sunny"}, run_id=root)

    assert mock_client.traces.upload.call_count == 1
    outputs = [e["payload"] for e in find_events(uploaded["events"], "agent.output")]
    assert {"output": {"output": "sunny"}, "log": "done"}.items() <= outputs[0].items()
    assert outputs[1]["agent_name"] == "weather_agent" and outputs[1]["output"] == {"output": "sunny"}
    assert len(outputs) == 2
