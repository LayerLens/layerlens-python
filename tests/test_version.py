"""``layerlens.__version__`` exists (R1 F4).

The install docs show ``import layerlens; print(layerlens.__version__)``; 1.11.0 and 2.0.0 raised
``AttributeError``, as ``__init__`` never imported ``_version``.
"""

from __future__ import annotations

import re
import sys
import subprocess
import importlib.metadata

import layerlens
from layerlens._version import __version__


def test_the_package_has_its_version() -> None:
    assert layerlens.__version__ == __version__
    assert re.fullmatch(r"\d+\.\d+\.\d+([.-]?\w+)*", layerlens.__version__)


def test_it_is_the_installed_distribution_version() -> None:
    assert layerlens.__version__ == importlib.metadata.version("layerlens")


def test_the_cli_prints_the_same_version() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['layerlens', '--version']; from layerlens.cli import main; main()",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.stdout.strip() == f"layerlens {layerlens.__version__}"


def test_star_import_does_not_bring_it_in() -> None:
    namespace: dict = {}
    exec("from layerlens import *", namespace)
    assert "__version__" not in namespace
