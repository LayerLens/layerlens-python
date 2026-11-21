"""The samples' graders run on an inexpensive model unless the caller names one.

``samples/core/quickstart.py`` creates its grader with ``create_judge`` and no model, and the
helper took the first model of the public catalogue. That order is alphabetical by key, so on a
catalogue led by ``anthropic/claude-fable-5`` one grader run of the quickstart's one-line trace
cost $0.111, where gpt-4o-mini costs $0.002 (docs E2E, 2026-11-11).
"""

from __future__ import annotations

import sys
import importlib.util
from types import ModuleType, SimpleNamespace
from typing import Any, List, Optional
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_helpers() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "samples_helpers_default_model", REPO_ROOT / "samples" / "_helpers.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _model(key: str, id: str) -> SimpleNamespace:
    return SimpleNamespace(key=key, id=id)


class _PublicModels:
    def __init__(self, catalogue: List[SimpleNamespace]) -> None:
        self.catalogue = catalogue

    def get(self, key: Optional[str] = None, **_: Any) -> SimpleNamespace:
        models = [m for m in self.catalogue if key is None or m.key == key]
        return SimpleNamespace(models=models)


def _client(catalogue: List[SimpleNamespace], project: Optional[List[SimpleNamespace]] = None) -> Any:
    return SimpleNamespace(
        public=SimpleNamespace(models=_PublicModels(catalogue)),
        models=SimpleNamespace(get=lambda **_: list(project or [])),
    )


@pytest.fixture
def helpers() -> ModuleType:
    return _load_helpers()


def test_the_default_grader_model_is_the_inexpensive_one_not_the_first_in_the_catalogue(helpers: ModuleType) -> None:
    client = _client([_model("anthropic/claude-fable-5", "fable"), _model("openai/gpt-4o-mini", "mini")])

    assert helpers.get_default_model_id(client) == "mini"


def test_a_catalogue_without_it_falls_back_to_its_first_model(helpers: ModuleType) -> None:
    client = _client([_model("google/gemini-2.5-flash", "flash"), _model("x/other", "other")])

    assert helpers.get_default_model_id(client) == "flash"


def test_with_no_public_models_the_project_models_are_used(helpers: ModuleType) -> None:
    client = _client([], project=[_model("custom/model", "custom")])

    assert helpers.get_default_model_id(client) == "custom"
