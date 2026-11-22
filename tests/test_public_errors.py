"""Every error the SDK raises can be caught as ``layerlens.<Name>`` (LAY-4429).

The errors guide has always shown ``except layerlens.RateLimitError``,
``layerlens.APIConnectionError`` and others that the package never exported, so the
``except`` clause itself raised ``AttributeError`` the moment a 429 came back. These tests
tie the docs to the code: every ``layerlens.<Name>Error`` a doc names, and every error
class the SDK defines, must be importable from ``layerlens`` as that very class.
"""

from __future__ import annotations

import re
from typing import List
from pathlib import Path

import pytest

import layerlens
from layerlens import _exceptions

ROOT = Path(__file__).resolve().parents[1]


def _documented() -> List[str]:
    files = [*sorted((ROOT / "docs").rglob("*.md")), ROOT / "README.md"]
    names = {
        name
        for path in files
        for name in re.findall(r"\blayerlens\.([A-Z][A-Za-z]*Error)\b", path.read_text(encoding="utf-8"))
    }
    return sorted(names)


def _defined() -> List[str]:
    return sorted(
        name
        for name, value in vars(_exceptions).items()
        if isinstance(value, type) and issubclass(value, Exception) and value.__module__ == _exceptions.__name__
    )


DOCUMENTED = _documented()
DEFINED = _defined()


def test_the_docs_and_the_sdk_name_the_errors_these_tests_rely_on() -> None:
    # Guards the scans above: an empty list would make every parametrized test vanish.
    assert {"RateLimitError", "APIConnectionError", "InternalServerError"} <= set(DOCUMENTED)
    assert {"RateLimitError", "APIStatusError", "APIResponseValidationError"} <= set(DEFINED)


@pytest.mark.parametrize("name", DOCUMENTED)
def test_every_error_the_docs_name_is_exported(name: str) -> None:
    assert name in layerlens.__all__
    assert getattr(layerlens, name) is getattr(_exceptions, name)


@pytest.mark.parametrize("name", DEFINED)
def test_every_error_the_sdk_raises_is_exported(name: str) -> None:
    assert name in layerlens.__all__
    assert getattr(layerlens, name) is getattr(_exceptions, name)


def test_a_rate_limit_can_be_caught_as_the_docs_show() -> None:
    import httpx

    response = httpx.Response(429, request=httpx.Request("GET", "https://api.test"))
    try:
        raise _exceptions.RateLimitError("Too Many Requests", response=response, body=None)
    except layerlens.RateLimitError as caught:
        assert isinstance(caught, layerlens.APIStatusError)
        assert isinstance(caught, layerlens.LayerLensError)
