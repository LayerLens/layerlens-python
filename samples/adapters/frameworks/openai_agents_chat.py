"""Sample: OpenAI Agents SDK adapter."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from adapters._shared import _build_client, capture_events  # type: ignore[import-not-found]


def main() -> None:
    try:
        from agents import Agent, Runner  # type: ignore[import-not-found]

        from layerlens.instrument.adapters.frameworks.openai_agents import (
            OpenAIAgentsAdapter,
        )
    except ImportError:
        print('Install: uv pip install --extra-index-url https://sdk.layerlens.ai/package "layerlens[openai-agents]"')
        return

    agent = Agent(name="demo", instructions="Answer in one word.")
    # The adapter's client is the LayerLens client, which uploads each run's trace (not the
    # agent): without LayerLens credentials there is nothing to upload with.
    client = _build_client()
    if client is None:
        print("LAYERLENS_API_KEY is not set: running the agent without the adapter.")
    else:
        OpenAIAgentsAdapter(client).connect()
    with capture_events("openai_agents"):
        result = Runner.run_sync(agent, "What colour is grass?")
        print("reply:", result.final_output)


if __name__ == "__main__":
    main()
