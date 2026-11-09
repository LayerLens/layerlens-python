"""The provider's own reason for an error, kept as ``error_code`` (LAY-4454).

Adapters record ``str(exc)``, and a provider SDK may change that text in any release:
openai 3.38 stopped printing the response body, so ``rate_limit_exceeded``,
``invalid_api_key`` and ``model_not_found`` vanished from the text while ``exc.code``
kept them. ``error_code`` keeps that machine-readable reason whatever the text says.

It is a category, like ``error_type``: :class:`CaptureConfig` keeps it as metadata under
``capture_content=False``, where the free-text ``error`` is stripped, and the event-schema
lock counts it as one. So it is only ever a short token, never text that could carry
content.

Where each SDK keeps it:

* openai, and the SDKs built on its errors (Azure OpenAI, LiteLLM): ``exc.code``;
* a provider's error body relayed as it came (groq, pydantic-ai's ``ModelHTTPError``):
  ``body["error"]["code"]``;
* AWS (botocore's ``ClientError``): ``exc.response["Error"]["Code"]``;
* Google Gen AI: ``exc.status`` (``RESOURCE_EXHAUSTED``; its ``exc.code`` is the HTTP status);
* anthropic: ``exc.type``, also ``body["error"]["type"]``.

A framework usually raises an error of its own, caused by the provider's, so the chain
of causes is searched too, nearest first, as a traceback lists it.
"""

from __future__ import annotations

import re
from typing import Any, Set, Dict, Iterator, Optional

__all__ = ["error_category", "error_code_of"]

# How many links of an error's chain of causes are searched.
MAX_CHAIN = 8

# A reason code is a short token. Anything longer, or with spaces, is text, not a code.
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}")


def error_category(exc: object) -> Dict[str, str]:
    """The fields that say what failed and survive ``capture_content=False``.

    ``error_type`` is the error's class name; ``error_code`` is there when the error, or
    an error that caused it, carries its provider's reason. Takes whatever a framework
    hands an adapter as the error, exception or not.
    """
    fields = {"error_type": type(exc).__name__}
    code = error_code_of(exc)
    if code is not None:
        fields["error_code"] = code
    return fields


def error_code_of(exc: object) -> Optional[str]:
    """The provider's reason for ``exc``, or for the nearest error that caused it; else ``None``.

    Never raises: an attribute that raises when read, or a value that raises when checked,
    counts as absent.
    """
    for link in _chain(exc):
        code = _own_code(link)
        if code is not None:
            return code
    return None


def _chain(exc: object) -> Iterator[object]:
    """``exc`` and the errors behind it, each once: its ``__cause__``, else its
    ``__context__`` unless suppressed (``raise ... from None``). A link that raises when
    read or checked counts as absent, as every attribute does here."""
    seen: Set[int] = set()
    link: Optional[object] = exc
    while link is not None and id(link) not in seen and len(seen) < MAX_CHAIN:
        seen.add(id(link))
        yield link
        try:
            if not isinstance(link, BaseException):
                return
        except Exception:  # isinstance reads __class__, which a proxy can make raise
            return
        cause = _read(link, "__cause__")
        link = (
            cause
            if cause is not None
            else (None if _read(link, "__suppress_context__") else _read(link, "__context__"))
        )


def _own_code(exc: object) -> Optional[str]:
    body = _read(exc, "body")
    for value in (
        _read(exc, "code"),
        _item(body, "error", "code"),
        _item(_read(exc, "response"), "Error", "Code"),
        _read(exc, "status"),
        _read(exc, "type"),
        _item(body, "error", "type"),
    ):
        code = _token(value)
        if code is not None:
            return code
    return None


def _read(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _item(value: Any, *keys: str) -> Any:
    try:
        for key in keys:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value
    except Exception:  # isinstance reads __class__, and a dict subclass's get() is its own
        return None


def _token(value: Any) -> Optional[str]:
    try:
        if isinstance(value, str) and _TOKEN.fullmatch(value):
            # A str subclass (a str-valued Enum) becomes its plain value; slicing makes a plain str.
            return value[:]
    except Exception:  # isinstance reads __class__, and a str subclass's slicing is its own
        pass
    return None
