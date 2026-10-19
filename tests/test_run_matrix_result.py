"""``tests/matrix/run_matrix.py`` records each row's result for the drift-canary report (LAY-4242).

The Adapter Drift Canary runs every row ``continue-on-error``; the row's result
file is the only thing that tells ``scripts/drift_canary_report.py`` it failed.
The verdicts come from real pytest terminal summaries.
"""

from __future__ import annotations

import sys
import json
import importlib.util
from types import ModuleType
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS = {"crewai": "1.15.28", "openai": "2.54.0"}


def _load(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


run_matrix = _load("tests/matrix/run_matrix.py", "run_matrix")
report = _load("scripts/drift_canary_report.py", "drift_canary_report")


@pytest.mark.parametrize(
    ("returncode", "summary", "red", "verdict"),
    [
        (0, "60 passed in 3.21s", False, "pass"),
        (1, "1 failed, 59 passed in 3.21s", True, "FAIL: 1 failed, 0 skipped of 60"),
        (1, "58 passed, 2 errors in 3.21s", True, "FAIL: 2 failed, 0 skipped of 60"),
        (0, "59 passed, 1 skipped in 3.21s", True, "FAIL (skips inside an installed-framework row): 1 skipped of 60"),
        (5, "no tests ran in 0.01s", True, "FAIL (collected nothing)"),
        (2, "Interrupted: 1 error during collection", True, "FAIL: 1 failed, 0 skipped of 1"),
    ],
)
def test_the_row_result_reflects_the_pytest_summary(returncode: int, summary: str, red: bool, verdict: str) -> None:
    result = run_matrix.row_result("crewai", VERSIONS, returncode, f"....\n{summary}\n")

    assert result == {"row": "crewai", "red": red, "verdict": verdict, "versions": VERSIONS}


def test_a_failed_install_is_red() -> None:
    assert run_matrix.install_failed_result("crewai") == {
        "row": "crewai",
        "red": True,
        "verdict": "FAIL: dependency install failed",
        "versions": {},
    }


def test_the_result_file_is_what_the_drift_report_reads(tmp_path: Path) -> None:
    run_matrix.write_result(
        str(tmp_path / "crewai.json"), run_matrix.row_result("crewai", VERSIONS, 1, "1 failed, 59 passed in 1s")
    )

    [row] = report.load_results(str(tmp_path), ["crewai"])

    assert (row.row, row.red, row.verdict, row.versions) == (
        "crewai",
        True,
        "FAIL: 1 failed, 0 skipped of 60",
        VERSIONS,
    )
    assert json.loads((tmp_path / "crewai.json").read_text())["red"] is True
