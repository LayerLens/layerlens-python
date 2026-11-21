"""``evaluations compare`` names the items that regressed when the dataset gives them no ``id``.

``evaluations run`` gave an item without an ``id`` the id ``item_{idx}_{random}``, new on every run,
and compare matches the two runs' items by id: a baseline and a candidate run of the same file never
shared one, so ``regressed_items`` and ``recovered_items`` were always empty, although the exit code
was right (docs E2E 2026-11-10, L2 B5). An item without an ``id`` is now ``item_{idx}``: the same in
every run of the same file.
"""

from __future__ import annotations

import sys
import json
import importlib
from typing import Any, Dict, List, Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from layerlens.cli._app import cli
from layerlens.datasets import Dataset, InMemoryDatasetStore

_MODULE = "layerlens_test_compare_targets"
_SOURCE = """
ANSWERS = {"q1": "4", "q2": "Paris", "q3": "blue"}


def good(question):
    return ANSWERS[question]


def worse(question):
    return "Lyon" if question == "q2" else ANSWERS[question]
"""

#: The docs' dataset file: {input, expected_output} items, no ids.
DATASET = [
    {"input": "q1", "expected_output": "4"},
    {"input": "q2", "expected_output": "Paris"},
    {"input": "q3", "expected_output": "blue"},
]


@pytest.fixture
def targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    mod_dir = tmp_path / "_targets"
    mod_dir.mkdir()
    (mod_dir / f"{_MODULE}.py").write_text(_SOURCE)
    monkeypatch.syspath_prepend(str(mod_dir))
    sys.modules.pop(_MODULE, None)
    importlib.invalidate_caches()
    try:
        yield _MODULE
    finally:
        sys.modules.pop(_MODULE, None)


def _run(tmp_path: Path, target: str, out: str) -> Path:
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(DATASET))
    path = tmp_path / out
    result = CliRunner().invoke(
        cli,
        ["--quiet", "evaluations", "run", "--dataset-id", "qa", "--dataset-file", str(dataset)]
        + ["--target", target, "--out", str(path)],
    )
    assert result.exit_code == 0, result.output
    return path


def _compare(baseline: Path, candidate: Path) -> Dict[str, Any]:
    result = CliRunner().invoke(cli, ["--quiet", "evaluations", "compare", str(baseline), str(candidate)])
    assert result.exit_code == 1, result.output
    comparison: Dict[str, Any] = json.loads(result.output[result.output.index("{") :])
    return comparison


def test_compare_names_the_item_that_regressed(tmp_path: Path, targets: str) -> None:
    baseline = _run(tmp_path, f"{targets}:good", "baseline.json")
    candidate = _run(tmp_path, f"{targets}:worse", "candidate.json")
    comparison = _compare(baseline, candidate)
    assert comparison["is_regression"] is True
    assert comparison["regressed_items"] == ["item_1"]


def test_compare_names_the_item_that_recovered(tmp_path: Path, targets: str) -> None:
    baseline = _run(tmp_path, f"{targets}:worse", "baseline.json")
    candidate = _run(tmp_path, f"{targets}:good", "candidate.json")
    result = CliRunner().invoke(cli, ["--quiet", "evaluations", "compare", str(baseline), str(candidate)])
    comparison = json.loads(result.output[result.output.index("{") :])
    assert comparison["recovered_items"] == ["item_1"]


def _ids(raw: List[Dict[str, Any]]) -> List[str]:
    store = InMemoryDatasetStore()
    dataset = store.create(Dataset(id="", name="qa"))
    version = store.import_items(dataset.id, raw)
    assert version is not None
    return [item.id for item in version.items]


def test_an_item_without_an_id_gets_the_same_id_in_every_import() -> None:
    assert _ids(DATASET) == _ids(DATASET) == ["item_0", "item_1", "item_2"]


def test_an_id_the_file_gives_is_kept_and_never_given_to_another_item() -> None:
    raw = [{"input": "a"}, {"id": "item_0", "input": "b"}, {"id": "q3", "input": "c"}]
    ids = _ids(raw)
    assert ids[1:] == ["item_0", "q3"]
    assert ids[0] != "item_0" and ids == _ids(raw)
    assert len(set(ids)) == 3
