"""M12 (SDK part): the pydantic-ai adapter printed a PydanticAIDeprecationWarning on every 2.x run.

pydantic-ai 2.x made ``AgentRunResult.usage`` a property and kept ``RunUsage.__call__`` for V1-style
``result.usage()``, with a deprecation warning on each call. The adapter called ``usage`` whenever it
was callable, so it took that path on every run. It now calls it only when it is the method
(pydantic-ai before 2), and reads the usage object as it is otherwise.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from tests._optional_deps import importorskip

pydantic_ai = importorskip("pydantic_ai")

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402

from layerlens.instrument.adapters.frameworks.pydantic_ai import PydanticAIAdapter  # noqa: E402

from .conftest import find_event, capture_framework_trace  # noqa: E402


class _Usage:
    input_tokens, output_tokens, requests = 7, 3, 1

    def __call__(self) -> "_Usage":
        warnings.warn("usage is a property, not a method", DeprecationWarning, stacklevel=2)
        return self


class _V2Result:
    usage = _Usage()


class _V1Result:
    def usage(self) -> _Usage:
        return _Usage()


@pytest.mark.parametrize("result", [_V2Result(), _V1Result()], ids=["2.x property", "1.x method"])
def test_usage_is_read_without_calling_a_usage_object(result: Any) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        tokens = PydanticAIAdapter._extract_usage(result)
    assert tokens["tokens_prompt"] == 7 and tokens["tokens_completion"] == 3 and tokens["model_requests"] == 1


def test_a_real_run_records_usage_without_a_deprecation_warning(mock_client: Any) -> None:
    uploaded = capture_framework_trace(mock_client)
    adapter = PydanticAIAdapter(mock_client)
    agent = Agent(model=TestModel(custom_output_text="Hello!"))
    adapter.connect(target=agent)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            agent.run_sync("hi")
    finally:
        adapter.disconnect()

    # PydanticAIDeprecationWarning (2.x) is a UserWarning; this one is what the adapter caused.
    deprecated = [w for w in caught if type(w.message).__name__ == "PydanticAIDeprecationWarning"]
    assert deprecated == [], [str(w.message) for w in deprecated]

    out = find_event(uploaded["events"], "agent.output")
    assert out["payload"]["tokens_prompt"] > 0 and out["payload"]["tokens_completion"] > 0
