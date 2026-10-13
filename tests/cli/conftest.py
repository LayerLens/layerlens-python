from __future__ import annotations

import inspect

import pytest
from click.testing import CliRunner


def _make_runner() -> CliRunner:
    """Create a CliRunner that keeps stderr separate across click versions.

    Click 8.2 and earlier default to ``mix_stderr=True``; 8.3+ dropped the
    flag entirely and always separates streams. Detect the signature so the
    suite works on both.
    """
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


@pytest.fixture
def runner():
    """Click CLI test runner."""
    return _make_runner()


@pytest.fixture(autouse=True)
def _no_credentials_from_the_environment(monkeypatch):
    """Keep a developer's real LayerLens key out of the CLI tests.

    The CLI reads its API key and base URL from the environment, so an exported
    key would otherwise reach a real API. Tests that need one set it explicitly.
    """
    for name in (
        "LAYERLENS_API_KEY",
        "LAYERLENS_BASE_URL",
        "LAYERLENS_STRATIX_API_KEY",
        "LAYERLENS_STRATIX_BASE_URL",
        "LAYERLENS_ATLAS_API_KEY",
        "LAYERLENS_ATLAS_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def cli_env():
    """Environment variables for CLI tests."""
    return {"LAYERLENS_API_KEY": "test-key-123"}
