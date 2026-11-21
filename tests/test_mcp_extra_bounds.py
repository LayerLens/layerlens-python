"""M9 (SDK part): ``layerlens[mcp]`` allowed every mcp from 0.9 on, with no upper bound.

The MCP adapter's tests pass on mcp 1.23 through 2.3.0 (the latest). mcp up to 1.21 does not import
with current pydantic (``eval_type_backport`` is gone from its internals), and 1.22 lacks the URL
elicitation types the adapter handles; mcp 3 does not exist yet. The extras now admit what was
checked: ``mcp>=1.23,<3`` (on Python 3.10+, as before).
"""

from __future__ import annotations

import re
from typing import List
from pathlib import Path

import pytest
from packaging.requirements import Requirement

PYPROJECT = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()


def _mcp_requirements(extra: str) -> List[Requirement]:
    block = re.search(rf"^{re.escape(extra)} = \[(.*?)^\]|^{re.escape(extra)} = \[(.*?)\]$", PYPROJECT, re.M | re.S)
    assert block, extra
    text = block.group(1) or block.group(2)
    return [Requirement(r) for r in re.findall(r'"([^"]+)"', text) if Requirement(r).name == "mcp"]


@pytest.mark.parametrize("extra", ["mcp", "all-protocols"])
def test_the_mcp_extras_admit_only_the_checked_releases(extra: str) -> None:
    (requirement,) = _mcp_requirements(extra)
    allowed = requirement.specifier
    assert "1.23.0" in allowed and "1.28.1" in allowed and "2.3.0" in allowed
    assert "0.9.1" not in allowed and "1.22.0" not in allowed and "3.0.0" not in allowed
    assert str(requirement.marker) == 'python_version >= "3.10"'
