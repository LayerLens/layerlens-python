"""m29: ``layerlens[openai-agents]`` on Python 3.9 installed an ``agents`` that would not import.

On 3.9 the extra resolves openai-agents 0.8.4 with pydantic 2.13.5, and ``import agents`` raised
``TypeError: Unable to evaluate type annotation 'float | None' ... install the eval_type_backport
package``. CI never saw it: the dev lock happens to hold eval-type-backport. The extra, and
``all-frameworks``, now install it on Python before 3.10 (checked in a fresh 3.9 venv).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement

PYPROJECT = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()


def _requirements(extra: str) -> "list[Requirement]":
    match = re.search(rf"^{re.escape(extra)} = \[(.*?)\]$", PYPROJECT, re.M) or re.search(
        rf"^{re.escape(extra)} = \[(.*?)^\]", PYPROJECT, re.M | re.S
    )
    assert match, extra
    return [Requirement(r) for r in re.findall(r'"([^"]+)"', match.group(1))]


@pytest.mark.parametrize("extra", ["openai-agents", "all-frameworks"])
def test_eval_type_backport_comes_with_openai_agents_before_python_3_10(extra: str) -> None:
    found = [r for r in _requirements(extra) if r.name == "eval-type-backport"]
    assert len(found) == 1, extra
    marker = found[0].marker or Marker("python_version >= '0'")
    assert marker.evaluate({"python_version": "3.9"}) and not marker.evaluate({"python_version": "3.10"})
