"""``copy()`` / ``with_options()`` keep every option the client was built with, and apply the ones given.

``copy(timeout=...)`` kept the client's own timeout (``self.timeout or timeout``, and an httpx
``Timeout`` is always true), so the alias's own example, ``client.with_options(timeout=10)``, did
nothing. The copy also went back to two retries, read ``LAYERLENS_TRUSTED_UPLOAD_HOSTS`` again, gave a
bearer-auth client's URL the session prefix twice, and read its organization and project again, the
first project instead of the one the client works in (docs re-run 2026-11-10, L1R-035b).
"""

from __future__ import annotations

from typing import Any, Type, Union

import httpx
import pytest

from layerlens import LayerLens, PublicClient, AsyncLayerLens, AsyncPublicClient
from layerlens._constants import DIRTY_ROUTER_PREFIX

BASE = "https://api.test/api/v1"
Client = Union[LayerLens, AsyncLayerLens]
CLIENTS = [LayerLens, AsyncLayerLens]


@pytest.fixture(autouse=True)
def _no_upload_hosts_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAYERLENS_TRUSTED_UPLOAD_HOSTS", raising=False)


def _built(cls: Type[Any], **options: Any) -> Any:
    client = cls(
        api_key="key-1",
        base_url=BASE,
        timeout=30.0,
        max_retries=0,
        trusted_upload_hosts=["minio.internal"],
        **options,
    )
    client.organization_id, client.project_id = "org-1", "proj-2"
    return client


@pytest.mark.parametrize("cls", CLIENTS)
def test_a_copy_keeps_every_option_and_the_project(cls: Type[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    client = _built(cls)
    monkeypatch.setenv("LAYERLENS_TRUSTED_UPLOAD_HOSTS", "elsewhere.example")
    copy = client.copy()
    assert copy is not client and copy.api_key == "key-1" and str(copy.base_url) == str(client.base_url)
    assert copy.timeout == httpx.Timeout(30.0)
    assert copy._max_retries == 0
    assert copy.trusted_upload_hosts == ["minio.internal"]
    assert (copy._organization_id, copy._project_id) == ("org-1", "proj-2")
    # The same URL given again, as another string object, is the same URL.
    same_url = "".join(["https://api.test", "/api/v1"])
    assert same_url == BASE and same_url is not BASE
    same = client.copy(base_url=same_url)
    assert (same._organization_id, same._project_id) == ("org-1", "proj-2")
    # Another key or URL may be another organization: that copy reads its own.
    for other in (client.copy(api_key="key-2"), client.copy(base_url="https://other.test/api/v1")):
        assert (other._organization_id, other._project_id) == (None, None)


@pytest.mark.parametrize("cls", CLIENTS)
def test_the_options_given_are_applied(cls: Type[Any]) -> None:
    client = _built(cls)
    assert client.with_options(timeout=10).timeout == httpx.Timeout(10)
    assert client.with_options(timeout=None).timeout == httpx.Timeout(None)
    assert client.with_options(max_retries=5)._max_retries == 5
    assert client.copy(trusted_upload_hosts=["s3.internal"]).trusted_upload_hosts == ["s3.internal"]
    other = client.copy(api_key="key-2", base_url="https://other.test/api/v1")
    assert (other.api_key, str(other.base_url).rstrip("/")) == ("key-2", "https://other.test/api/v1")
    assert (client.timeout, client._max_retries, client.api_key) == (httpx.Timeout(30.0), 0, "key-1")


@pytest.mark.parametrize("cls", CLIENTS)
def test_a_bearer_auth_copy_keeps_its_url(cls: Type[Any]) -> None:
    client = cls(api_key="token", base_url=BASE, use_bearer_auth=True)
    copy = client.with_options(timeout=5)
    assert str(copy.base_url) == str(client.base_url)
    assert str(copy.base_url).rstrip("/").endswith(DIRTY_ROUTER_PREFIX)
    assert DIRTY_ROUTER_PREFIX * 2 not in str(copy.base_url)
    assert copy.auth_headers == {"Authorization": "Bearer token"}


@pytest.mark.parametrize("cls", [PublicClient, AsyncPublicClient])
def test_a_public_client_copy_keeps_its_options_and_applies_the_ones_given(cls: Type[Any]) -> None:
    client = cls(api_key="key-1", base_url=BASE, timeout=30.0, max_retries=0)
    copy = client.copy()
    assert (copy.api_key, copy.timeout, copy._max_retries) == ("key-1", httpx.Timeout(30.0), 0)
    assert client.with_options(timeout=10).timeout == httpx.Timeout(10)
    assert client.with_options(max_retries=3)._max_retries == 3
