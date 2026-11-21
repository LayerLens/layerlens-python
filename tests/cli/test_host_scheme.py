"""B3: ``--host``/``--port`` spoke plain HTTP on every port but 443, so the key crossed the network in clear.

The docs' "custom host" example, ``layerlens --host my-instance.example.com --port 8443 trace list``,
sent ``GET /api/v1/organizations`` with the ``x-api-key`` header over plain HTTP to port 8443. The
CLI now speaks HTTPS on every port. It speaks plain HTTP only to a loopback host (``localhost``,
``127.0.0.1``, ``::1``) or when ``--host`` is an ``http://`` URL; ``--host`` also takes an
``https://`` URL. Requests are captured at the httpx layer, so these tests assert what leaves the
process.
"""

# ruff: noqa: ARG002  # fixtures used for their monkeypatch side effects

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Set, List, Optional
from pathlib import Path

import click
import httpx
import pytest
from click.testing import CliRunner

from layerlens.cli._app import cli
from layerlens.cli._command import base_url_for

from .conftest import _make_runner

ORG = {"id": "org-1", "name": "Acme", "projects": [{"id": "proj-1", "name": "Default"}]}


@pytest.mark.parametrize(
    ("host", "port", "url"),
    [
        # Any host but this machine: HTTPS, whatever the port.
        ("my-instance.example.com", 8443, "https://my-instance.example.com:8443/api/v1"),
        ("api.layerlens.ai", None, "https://api.layerlens.ai/api/v1"),
        ("api.layerlens.ai", 443, "https://api.layerlens.ai/api/v1"),
        ("api.example.com", 80, "https://api.example.com:80/api/v1"),
        ("api.example.com", 9443, "https://api.example.com:9443/api/v1"),
        ("10.0.0.7", 8080, "https://10.0.0.7:8080/api/v1"),
        ("fe80::1", 8443, "https://[fe80::1]:8443/api/v1"),
        # This machine: plain HTTP, except on 443.
        ("localhost", 8080, "http://localhost:8080/api/v1"),
        ("localhost", None, "http://localhost/api/v1"),
        ("LOCALHOST", 8080, "http://localhost:8080/api/v1"),
        ("127.0.0.1", 8080, "http://127.0.0.1:8080/api/v1"),
        ("127.0.0.1", 80, "http://127.0.0.1/api/v1"),
        ("127.0.0.1", 443, "https://127.0.0.1/api/v1"),
        ("::1", 8080, "http://[::1]:8080/api/v1"),
        ("[::1]", 8080, "http://[::1]:8080/api/v1"),
        ("[::1]", None, "http://[::1]/api/v1"),
        # A URL says its scheme, and may carry the port and a path.
        ("http://api.example.com", None, "http://api.example.com/api/v1"),
        ("http://api.example.com:8080", None, "http://api.example.com:8080/api/v1"),
        ("http://api.example.com", 8080, "http://api.example.com:8080/api/v1"),
        ("https://api.example.com", None, "https://api.example.com/api/v1"),
        ("https://api.example.com:8443", None, "https://api.example.com:8443/api/v1"),
        ("https://api.example.com", 8443, "https://api.example.com:8443/api/v1"),
        ("https://api.example.com:8443", 8443, "https://api.example.com:8443/api/v1"),
        ("HTTPS://api.example.com/", None, "https://api.example.com/api/v1"),
        ("https://api.example.com/custom/api/v1/", None, "https://api.example.com/custom/api/v1"),
        ("https://localhost:8443", None, "https://localhost:8443/api/v1"),
        ("http://localhost:8080", None, "http://localhost:8080/api/v1"),
        ("http://[::1]:8080", None, "http://[::1]:8080/api/v1"),
        (None, None, None),
        (None, 8443, None),
    ],
)
def test_the_url_each_host_and_port_make(host: Optional[str], port: Optional[int], url: Optional[str]) -> None:
    assert base_url_for(host, port) == url


@pytest.mark.parametrize(
    ("host", "port", "says"),
    [
        ("ftp://api.example.com", None, "neither a host nor an http:// or https:// URL"),
        ("https://api.example.com:9000", 8443, "differs from the port in --host"),
        ("https://api.example.com:http", None, "not a number"),
        ("https://", None, "names no host"),
    ],
)
def test_a_host_it_cannot_use_is_a_usage_error(host: str, port: Optional[int], says: str) -> None:
    with pytest.raises(click.BadParameter, match=says):
        base_url_for(host, port)


@pytest.fixture
def runner() -> CliRunner:
    return _make_runner()


@pytest.fixture
def no_stored_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds_dir = tmp_path / ".layerlens"
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_DIR", creds_dir)
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_FILE", creds_dir / "credentials")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fake LayerLens API that records every request the CLI sends."""
    state = SimpleNamespace(requests=[])

    def send(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        state.requests.append(request)
        if request.url.path.endswith("/organizations"):
            return httpx.Response(200, json={"data": ORG}, request=request)
        if request.url.path.endswith("/traces"):
            return httpx.Response(
                200, json={"status": "success", "data": {"traces": [], "count": 0, "total_count": 0}}, request=request
            )
        return httpx.Response(404, json={"error": "unexpected path"}, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    return state


def _stderr(result: Any) -> str:
    try:
        return result.stderr or ""
    except (ValueError, AttributeError):
        return ""


def _origins(requests: List[httpx.Request]) -> Set[str]:
    assert requests, "the CLI sent no request"
    return {f"{r.url.scheme}://{r.url.netloc.decode()}" for r in requests}


@pytest.mark.parametrize(
    "argv",
    [
        ["--host", "my-instance.example.com", "--port", "8443", "trace", "list"],
        ["trace", "list", "--host", "my-instance.example.com", "--port", "8443"],
        ["--host", "my-instance.example.com", "trace", "list", "--port", "8443"],
        ["--host", "https://my-instance.example.com:8443", "trace", "list"],
    ],
    ids=["before", "after", "split", "url"],
)
def test_the_docs_custom_host_example_sends_the_key_over_https(
    argv: List[str], runner: CliRunner, no_stored_login: None, server: SimpleNamespace
) -> None:
    result = runner.invoke(cli, argv, env={"LAYERLENS_API_KEY": "k"})

    assert result.exit_code == 0, result.output
    assert _origins(server.requests) == {"https://my-instance.example.com:8443"}
    assert all(r.headers.get("x-api-key") == "k" for r in server.requests)


def test_an_http_url_is_spoken_plain_http(runner: CliRunner, no_stored_login: None, server: SimpleNamespace) -> None:
    result = runner.invoke(
        cli, ["--host", "http://gateway.internal:8080", "trace", "list"], env={"LAYERLENS_API_KEY": "k"}
    )

    assert result.exit_code == 0, result.output
    assert _origins(server.requests) == {"http://gateway.internal:8080"}


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["--host", "ftp://api.example.com", "trace", "list"], "neither a host nor an http:// or https:// URL"),
        (["--host", "https://api.example.com:9000", "--port", "8443", "trace", "list"], "differs from the port"),
        (["--host", "https://api.example.com:9000", "trace", "list", "--port", "8443"], "differs from the port"),
    ],
)
def test_a_host_it_cannot_use_exits_2_and_sends_nothing(
    argv: List[str], says: str, runner: CliRunner, no_stored_login: None, server: SimpleNamespace
) -> None:
    result = runner.invoke(cli, argv, env={"LAYERLENS_API_KEY": "k"})

    assert result.exit_code == 2, result.output
    assert says in result.output + _stderr(result)
    assert server.requests == []
