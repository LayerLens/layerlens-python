"""Backward compatibility for the Stratix -> LayerLens rename (LAY-4220).

The canonical names are ``LayerLens`` / ``AsyncLayerLens`` / ``LayerLensError``
and the ``LAYERLENS_API_KEY`` / ``LAYERLENS_BASE_URL`` environment variables.
Every name and variable from earlier releases must keep working unchanged, be
the *same object* as its replacement (so ``isinstance`` and ``except`` behave
exactly as before), and tell the developer what to rename via a
``DeprecationWarning`` attributed to their own code.
"""

from __future__ import annotations

import warnings
import importlib
from typing import Any, Iterator
from unittest.mock import Mock, patch

import httpx
import pytest

import layerlens
from layerlens import (
    LayerLens,
    PublicClient,
    NotFoundError,
    AsyncLayerLens,
    LayerLensError,
    AsyncPublicClient,
)

ALL_ENV_VARS = (
    "LAYERLENS_API_KEY",
    "LAYERLENS_BASE_URL",
    "LAYERLENS_STRATIX_API_KEY",
    "LAYERLENS_STRATIX_BASE_URL",
    "LAYERLENS_ATLAS_API_KEY",
    "LAYERLENS_ATLAS_BASE_URL",
)

LEGACY_CLASS_NAMES = [
    ("layerlens", "Stratix", "LayerLens"),
    ("layerlens", "AsyncStratix", "AsyncLayerLens"),
    ("layerlens", "Atlas", "LayerLens"),
    ("layerlens", "AsyncAtlas", "AsyncLayerLens"),
    ("layerlens", "StratixError", "LayerLensError"),
    ("layerlens", "AtlasError", "LayerLensError"),
    ("layerlens._client", "Stratix", "LayerLens"),
    ("layerlens._client", "AsyncStratix", "AsyncLayerLens"),
    ("layerlens._client", "Atlas", "LayerLens"),
    ("layerlens._client", "AsyncAtlas", "AsyncLayerLens"),
    ("layerlens._exceptions", "StratixError", "LayerLensError"),
    ("layerlens._exceptions", "AtlasError", "LayerLensError"),
]


def _deprecations(records: Any) -> list[warnings.WarningMessage]:
    return [r for r in records if issubclass(r.category, DeprecationWarning) and "deprecated" in str(r.message)]


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
    with patch.object(LayerLens, "_get_organization", return_value=org), patch.object(
        AsyncLayerLens, "_get_organization", return_value=org
    ):
        yield


class TestLegacyNames:
    @pytest.mark.parametrize("module_name, legacy, replacement", LEGACY_CLASS_NAMES)
    def test_legacy_name_is_the_replacement_object_and_warns(
        self, module_name: str, legacy: str, replacement: str
    ) -> None:
        module = importlib.import_module(module_name)

        with pytest.warns(DeprecationWarning) as records:
            obj = getattr(module, legacy)

        assert obj is getattr(layerlens, replacement)
        (warning,) = _deprecations(records)
        assert f"{module_name}.{legacy} is deprecated" in str(warning.message)
        assert f"use layerlens.{replacement} instead" in str(warning.message)
        # Attributed to the line that touched the legacy name, not to SDK internals.
        assert warning.filename == __file__

    def test_from_import_warns_at_the_importing_line(self) -> None:
        with pytest.warns(DeprecationWarning) as records:
            from layerlens import Stratix

        assert Stratix is LayerLens
        (warning,) = _deprecations(records)
        assert warning.filename == __file__

    def test_legacy_exception_still_catches_errors_the_sdk_raises(self) -> None:
        with pytest.warns(DeprecationWarning):
            from layerlens import StratixError

        response = httpx.Response(404, request=httpx.Request("GET", "https://api.layerlens.ai/api/v1/x"))
        with pytest.raises(StratixError):
            raise NotFoundError("missing", response=response, body=None)
        with pytest.raises(StratixError):
            raise LayerLensError("boom")

    def test_isinstance_holds_across_old_and_new_names(self, no_org_fetch: None) -> None:
        _ = no_org_fetch
        with pytest.warns(DeprecationWarning):
            from layerlens import Stratix

        client = LayerLens(api_key="k")
        assert isinstance(client, Stratix)

    def test_no_legacy_name_is_exported_in_all(self) -> None:
        legacy = {"Stratix", "AsyncStratix", "Atlas", "AsyncAtlas", "StratixError", "AtlasError"}
        assert legacy.isdisjoint(layerlens.__all__)
        assert {"LayerLens", "AsyncLayerLens", "LayerLensError"} <= set(layerlens.__all__)

    def test_star_import_emits_no_deprecation_warning(self) -> None:
        namespace: dict[str, Any] = {}
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            exec("from layerlens import *", namespace)

        assert _deprecations(records) == []
        assert namespace["LayerLens"] is LayerLens

    def test_unknown_attribute_still_raises_attribute_error(self) -> None:
        for module_name in ("layerlens", "layerlens._client", "layerlens._exceptions"):
            module = importlib.import_module(module_name)
            with pytest.raises(AttributeError):
                _ = module.LayerLnes

    def test_client_aliases_are_unchanged(self) -> None:
        assert layerlens.Client is LayerLens
        assert layerlens.AsyncClient is AsyncLayerLens


class TestApiKeyEnvironment:
    @pytest.mark.parametrize("client_cls", [LayerLens, AsyncLayerLens, PublicClient, AsyncPublicClient])
    def test_canonical_variable_is_read_without_warning(
        self, client_cls: Any, clean_env: pytest.MonkeyPatch, no_org_fetch: None
    ) -> None:
        _ = no_org_fetch
        clean_env.setenv("LAYERLENS_API_KEY", "new-key")

        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            client = client_cls()

        assert client.api_key == "new-key"
        assert _deprecations(records) == []

    @pytest.mark.parametrize("client_cls", [LayerLens, AsyncLayerLens, PublicClient, AsyncPublicClient])
    @pytest.mark.parametrize("legacy", ["LAYERLENS_STRATIX_API_KEY", "LAYERLENS_ATLAS_API_KEY"])
    def test_legacy_variable_still_works_and_warns(
        self, client_cls: Any, legacy: str, clean_env: pytest.MonkeyPatch, no_org_fetch: None
    ) -> None:
        _ = no_org_fetch
        clean_env.setenv(legacy, "old-key")

        with pytest.warns(DeprecationWarning) as records:
            client = client_cls()

        assert client.api_key == "old-key"
        (warning,) = _deprecations(records)
        assert legacy in str(warning.message)
        assert "LAYERLENS_API_KEY" in str(warning.message)
        assert warning.filename == __file__

    @pytest.mark.parametrize("client_cls", [LayerLens, PublicClient])
    def test_canonical_variable_wins_over_legacy_silently(
        self, client_cls: Any, clean_env: pytest.MonkeyPatch, no_org_fetch: None
    ) -> None:
        _ = no_org_fetch
        clean_env.setenv("LAYERLENS_API_KEY", "new-key")
        clean_env.setenv("LAYERLENS_STRATIX_API_KEY", "old-key")
        clean_env.setenv("LAYERLENS_ATLAS_API_KEY", "older-key")

        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            client = client_cls()

        assert client.api_key == "new-key"
        assert _deprecations(records) == []

    def test_stratix_variable_wins_over_atlas_variable(self, clean_env: pytest.MonkeyPatch, no_org_fetch: None) -> None:
        _ = no_org_fetch
        clean_env.setenv("LAYERLENS_STRATIX_API_KEY", "old-key")
        clean_env.setenv("LAYERLENS_ATLAS_API_KEY", "older-key")

        with pytest.warns(DeprecationWarning):
            client = LayerLens()

        assert client.api_key == "old-key"

    def test_empty_canonical_variable_counts_as_unset(self, clean_env: pytest.MonkeyPatch, no_org_fetch: None) -> None:
        _ = no_org_fetch
        clean_env.setenv("LAYERLENS_API_KEY", "")
        clean_env.setenv("LAYERLENS_STRATIX_API_KEY", "old-key")

        with pytest.warns(DeprecationWarning):
            client = LayerLens()

        assert client.api_key == "old-key"

    def test_explicit_api_key_never_reads_the_environment(
        self, clean_env: pytest.MonkeyPatch, no_org_fetch: None
    ) -> None:
        _ = no_org_fetch
        clean_env.setenv("LAYERLENS_STRATIX_API_KEY", "old-key")

        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            client = LayerLens(api_key="explicit")

        assert client.api_key == "explicit"
        assert _deprecations(records) == []

    @pytest.mark.parametrize("client_cls", [LayerLens, AsyncLayerLens, PublicClient, AsyncPublicClient])
    def test_missing_key_error_names_the_canonical_variable(
        self, client_cls: Any, clean_env: pytest.MonkeyPatch, no_org_fetch: None
    ) -> None:
        _ = (clean_env, no_org_fetch)
        with pytest.raises(LayerLensError) as excinfo:
            client_cls()

        assert "LAYERLENS_API_KEY" in str(excinfo.value)
        assert "STRATIX" not in str(excinfo.value)


class TestBaseUrlEnvironment:
    @pytest.mark.parametrize("client_cls", [LayerLens, AsyncLayerLens, PublicClient, AsyncPublicClient])
    def test_canonical_variable_is_read_without_warning(
        self, client_cls: Any, clean_env: pytest.MonkeyPatch, no_org_fetch: None
    ) -> None:
        _ = no_org_fetch
        clean_env.setenv("LAYERLENS_BASE_URL", "https://new.example.com/api/v1")

        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            client = client_cls(api_key="k")

        assert str(client.base_url).rstrip("/") == "https://new.example.com/api/v1"
        assert _deprecations(records) == []

    @pytest.mark.parametrize("client_cls", [LayerLens, AsyncLayerLens, PublicClient, AsyncPublicClient])
    @pytest.mark.parametrize("legacy", ["LAYERLENS_STRATIX_BASE_URL", "LAYERLENS_ATLAS_BASE_URL"])
    def test_legacy_variable_still_works_and_warns(
        self, client_cls: Any, legacy: str, clean_env: pytest.MonkeyPatch, no_org_fetch: None
    ) -> None:
        _ = no_org_fetch
        clean_env.setenv(legacy, "https://old.example.com/api/v1")

        with pytest.warns(DeprecationWarning) as records:
            client = client_cls(api_key="k")

        assert str(client.base_url).rstrip("/") == "https://old.example.com/api/v1"
        (warning,) = _deprecations(records)
        assert legacy in str(warning.message)
        assert "LAYERLENS_BASE_URL" in str(warning.message)
        assert warning.filename == __file__

    def test_canonical_variable_wins_over_legacy_silently(
        self, clean_env: pytest.MonkeyPatch, no_org_fetch: None
    ) -> None:
        _ = no_org_fetch
        clean_env.setenv("LAYERLENS_BASE_URL", "https://new.example.com/api/v1")
        clean_env.setenv("LAYERLENS_STRATIX_BASE_URL", "https://old.example.com/api/v1")

        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter("always")
            client = LayerLens(api_key="k")

        assert str(client.base_url).rstrip("/") == "https://new.example.com/api/v1"
        assert _deprecations(records) == []

    def test_default_base_url_when_nothing_is_set(self, clean_env: pytest.MonkeyPatch, no_org_fetch: None) -> None:
        _ = (clean_env, no_org_fetch)
        client = LayerLens(api_key="k")

        assert str(client.base_url).rstrip("/") == "https://api.layerlens.ai/api/v1"


class TestAguiMapper:
    def test_canonical_mapper_returns_layerlens_event(self) -> None:
        from layerlens.instrument.adapters.protocols.agui import map_agui_to_layerlens

        assert map_agui_to_layerlens("RUN_ERROR") == {"layerlens_event": "agent.error", "category": "lifecycle"}
        assert map_agui_to_layerlens("NOT_A_TYPE") == {
            "layerlens_event": "protocol.stream.event",
            "category": "unknown",
        }

    @pytest.mark.parametrize(
        "module_name",
        [
            "layerlens.instrument.adapters.protocols.agui",
            "layerlens.instrument.adapters.protocols.agui.event_mapper",
        ],
    )
    def test_legacy_mapper_warns_and_keeps_its_old_shape(self, module_name: str) -> None:
        legacy = importlib.import_module(module_name).map_agui_to_stratix

        with pytest.warns(DeprecationWarning) as records:
            mapping = legacy("TOOL_CALL_START")

        assert mapping == {"stratix_event": "tool.call", "category": "tool"}
        (warning,) = _deprecations(records)
        assert "map_agui_to_layerlens" in str(warning.message)
        assert warning.filename == __file__
