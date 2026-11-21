"""The dev environment installs the crewai and dspy the floor suites need (LAY-4242).

The crewai suites skip below crewai 1.14 and the dspy suites skip below dspy 3. A
plain ``uv lock --upgrade`` picks crewai 1.6 and dspy 2.6 (one lock holds every
extra and it trades versions between them), and with the old lock ``run-tests``
skipped those modules entirely. The ``dev`` dependency group in pyproject.toml
pins floors to stop that; this fails if the environment drops below them again.
"""

from __future__ import annotations

import sys
import platform
from importlib.metadata import PackageNotFoundError, version

import pytest
from packaging.version import Version


@pytest.mark.skipif(sys.version_info < (3, 10), reason="every crewai and dspy release needs Python 3.10+")
@pytest.mark.parametrize(
    ("package", "floor"),
    [
        pytest.param(
            "crewai",
            "1.14",
            id="crewai",
            marks=pytest.mark.skipif(
                sys.platform == "darwin" and platform.machine() == "x86_64",
                reason="crewai >= 1.14 needs lancedb, which ships no Intel-mac wheels",
            ),
        ),
        pytest.param("dspy", "3", id="dspy"),
    ],
)
def test_the_floor_suites_run_instead_of_skipping(package: str, floor: str) -> None:
    try:
        installed = version(package)
    except PackageNotFoundError:
        pytest.fail(f"{package} is not installed; run `uv sync --all-extras` (CONTRIBUTING.md)")

    assert Version(installed) >= Version(floor), (
        f"{package} {installed} is installed, so its floor suites skip (they need >= {floor}). "
        'Keep the floors in the pyproject.toml "dev" group and re-lock.'
    )
