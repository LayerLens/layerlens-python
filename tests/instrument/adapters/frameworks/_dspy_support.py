"""A real ``dspy.LM`` whose provider is a local server — the network is the only thing faked.

The usage-bearing tests need a call that reports real token usage through DSPy's
own path: the LM's engine parses the provider response and appends its usage to
``LM.history``, which the adapter reads. ``DummyLM`` can't do that from dspy 3.4
(its engine ignores a ``forward()`` override and reports zero usage), and an
injected HTTP client can't either (dspy 3.4 deep-copies the LM's kwargs). So
:func:`served_lm` points a real ``dspy.LM`` at ``recorded_http_server``, which
answers every call with an OpenAI chat completion carrying the given fields and
usage. dspy 3.4's default engine sends both ``openai/*`` and ``ollama/*`` models
to that OpenAI-compatible endpoint.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple, Iterator
from contextlib import contextmanager

import dspy

from ..._recorded import recorded_http_server


def chat_completion(fields: Dict[str, str], usage: Dict[str, int]) -> Dict[str, Any]:
    """An OpenAI chat-completion body whose content is dspy ChatAdapter output for ``fields``."""
    content = "\n\n".join(f"[[ ## {name} ## ]]\n{value}" for name, value in fields.items())
    return {
        "id": "chatcmpl-layerlens-test",
        "object": "chat.completion",
        "created": 0,
        "model": "served",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content + "\n\n[[ ## completed ## ]]"},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


@contextmanager
def served_lm(model: str, fields: Dict[str, str], usage: Dict[str, int]) -> Iterator[Tuple[Any, List[Dict[str, Any]]]]:
    """Yield ``(lm, requests)``: a real ``dspy.LM`` for ``model`` and the requests it sent."""
    fixture = {"interactions": [{"response": {"status_code": 200, "json": chat_completion(fields, usage)}}]}
    with recorded_http_server(fixture) as (api_base, requests):
        yield dspy.LM(model, api_base=api_base, api_key="unused", cache=False), requests
