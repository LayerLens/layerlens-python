"""Sample helpers find credentials exactly the way the SDK does (LAY-4220).

The SDK still honours the pre-rename ``LAYERLENS_STRATIX_*`` variables, and the
migration guide promises existing configurations keep working. A sample helper
that pre-checks only ``LAYERLENS_API_KEY`` silently skips the upload for those
users: events print, nothing is sent, and nothing says why.
"""

from __future__ import annotations

import sys
import importlib.util
from types import ModuleType
from typing import Iterator
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from layerlens import LayerLens

REPO_ROOT = Path(__file__).resolve().parents[1]

ALL_ENV_VARS = (
    "LAYERLENS_API_KEY",
    "LAYERLENS_BASE_URL",
    "LAYERLENS_STRATIX_API_KEY",
    "LAYERLENS_STRATIX_BASE_URL",
    "LAYERLENS_ATLAS_API_KEY",
    "LAYERLENS_ATLAS_BASE_URL",
)


def _load(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def no_org_fetch() -> Iterator[None]:
    org = Mock()
    org.id = "org-123"
    org.projects = [Mock(id="proj-456")]
    with patch.object(LayerLens, "_get_organization", return_value=org):
        yield


class TestAdapterSamplesSharedHelper:
    @pytest.fixture
    def shared(self) -> ModuleType:
        return _load("samples/adapters/_shared.py", "_adapter_samples_shared_under_test")

    @pytest.mark.parametrize("variable", ["LAYERLENS_API_KEY", "LAYERLENS_STRATIX_API_KEY", "LAYERLENS_ATLAS_API_KEY"])
    def test_any_key_the_sdk_honours_enables_the_upload(
        self, shared: ModuleType, clean_env: pytest.MonkeyPatch, no_org_fetch: None, variable: str
    ) -> None:
        _ = no_org_fetch
        clean_env.setenv(variable, "key-123")

        client = shared._build_client()

        assert isinstance(client, LayerLens)
        assert client.api_key == "key-123"

    def test_no_key_runs_offline(self, shared: ModuleType, clean_env: pytest.MonkeyPatch, no_org_fetch: None) -> None:
        _ = (clean_env, no_org_fetch)
        assert shared._build_client() is None


class TestVendoredLangGraphRunner:
    @pytest.fixture
    def runner(self) -> ModuleType:
        pytest.importorskip("langchain_openai", reason="the vendored LangGraph sample imports it at module load")
        return _load(
            "samples/vendor/langgraph/customer-support-with-handoffs/run_instrumented.py",
            "_vendored_langgraph_runner_under_test",
        )

    @pytest.mark.parametrize(
        "key_var, url_var",
        [("LAYERLENS_API_KEY", "LAYERLENS_BASE_URL"), ("LAYERLENS_STRATIX_API_KEY", "LAYERLENS_STRATIX_BASE_URL")],
    )
    def test_any_configuration_the_sdk_honours_builds_a_client(
        self, runner: ModuleType, clean_env: pytest.MonkeyPatch, no_org_fetch: None, key_var: str, url_var: str
    ) -> None:
        _ = no_org_fetch
        clean_env.setenv(key_var, "key-123")
        clean_env.setenv(url_var, "https://dev.example.com/api/v1")

        client, reason = runner.build_real_client()

        assert reason is None
        assert isinstance(client, LayerLens)

    def test_still_refuses_the_production_default(
        self, runner: ModuleType, clean_env: pytest.MonkeyPatch, no_org_fetch: None
    ) -> None:
        _ = no_org_fetch
        clean_env.setenv("LAYERLENS_API_KEY", "key-123")

        client, reason = runner.build_real_client()

        assert client is None
        assert "LAYERLENS_BASE_URL" in reason
