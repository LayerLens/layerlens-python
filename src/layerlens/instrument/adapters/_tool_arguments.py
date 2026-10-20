"""``tool.call`` arguments as one type, whichever adapter or SDK version produced them.

Tool-call arguments reach the adapters in two forms: the provider's raw JSON string
(OpenAI's wire format, which langchain-openai before 1.7 also copied into
``additional_kwargs["tool_calls"]``) or an already-parsed object (Anthropic, Vertex,
Bedrock, and LangChain's own ``message.tool_calls``). ``tool.call`` carries the parsed
object, so a trace consumer never has to branch on which adapter, or which version of
its framework, wrote the event. This is the one parser for the string form, shared by
the adapters that receive it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log: logging.Logger = logging.getLogger(__name__)

#: The most of an undecodable argument string that reaches a log line.
_LOG_SNIPPET_CHARS = 200


def parse_tool_arguments(raw: Any) -> Any:
    """Return tool-call arguments decoded from JSON when *raw* is a JSON string.

    Anything that is not a string (an already-parsed object, ``None``) and a blank
    string come back unchanged. So does a string Python cannot decode, with a WARNING
    (LAY-3331): the tool call still reaches the trace with exactly what the model sent,
    and instrumentation never raises into the caller's code. That covers malformed JSON
    (``JSONDecodeError``, a ``ValueError``) and valid JSON past the interpreter's
    limits, which raises other errors: an integer longer than ``int()`` converts
    (``ValueError``) or nesting deeper than the decoder recurses (``RecursionError``).
    The log line carries at most the first 200 characters, which keeps arbitrary tool
    arguments out of long-lived log files.
    """
    if not isinstance(raw, str) or not raw.strip():
        return raw
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:
        snippet = raw if len(raw) <= _LOG_SNIPPET_CHARS else raw[:_LOG_SNIPPET_CHARS] + "..."
        log.warning("malformed tool_call JSON arguments (%s): %r", exc, snippet)
        return raw
