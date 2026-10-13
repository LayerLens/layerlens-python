"""CLI side of the Stratix -> LayerLens rename (LAY-4220).

Covers which credential the CLI sends and *how*: an API key must go out as
``x-api-key`` on the normal ``/api/v1`` router, because the ``/dgklmnr`` router
only verifies Cognito login tokens. Every request is captured at the httpx
layer, so these tests assert what actually leaves the process.
"""

# ruff: noqa: ARG002  # fixtures used for their monkeypatch side effects

from __future__ import annotations

import os
import re
import sys
import time
import subprocess
from types import SimpleNamespace
from typing import Any, List
from pathlib import Path

import click
import httpx
import pytest

from layerlens.cli._app import cli
from layerlens.cli._auth import save_credentials
from layerlens.cli._banner import banner

from .conftest import _make_runner

REPO_ROOT = Path(__file__).resolve().parents[2]

ORG = {"id": "org-1", "name": "Acme", "projects": [{"id": "proj-1", "name": "Default"}]}

ALL_ENV_VARS = (
    "LAYERLENS_API_KEY",
    "LAYERLENS_BASE_URL",
    "LAYERLENS_STRATIX_API_KEY",
    "LAYERLENS_STRATIX_BASE_URL",
    "LAYERLENS_ATLAS_API_KEY",
    "LAYERLENS_ATLAS_BASE_URL",
)


def _stderr(result: Any) -> str:
    try:
        return result.stderr or ""
    except (ValueError, AttributeError):
        return ""


@pytest.fixture
def runner():
    return _make_runner()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def creds_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    creds_dir = tmp_path / ".layerlens"
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_DIR", creds_dir)
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_FILE", creds_dir / "credentials")
    return creds_dir


@pytest.fixture
def stored_login(creds_dir: Path) -> None:
    save_credentials(
        {
            "access_token": "access-tok",
            "id_token": "id-tok",
            "refresh_token": "refresh-tok",
            "expires_at": time.time() + 3600,
            "token_type": "Bearer",
            "user": {"email": "someone@example.com"},
        }
    )


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fake LayerLens API that records every request the CLI sends."""
    state = SimpleNamespace(requests=[], status=200)

    def send(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        state.requests.append(request)
        if state.status != 200:
            return httpx.Response(state.status, json={"error": "Invalid x-api-key header"}, request=request)
        path = request.url.path
        if path.endswith("/organizations"):
            # The login router lists organizations; the API-key router returns the key's own.
            body = {"data": [ORG]} if "/dgklmnr/" in path else {"data": ORG}
            return httpx.Response(200, json=body, request=request)
        if path.endswith("/traces"):
            return httpx.Response(200, json={"data": {"traces": [], "count": 0, "total_count": 0}}, request=request)
        return httpx.Response(404, json={"error": f"unexpected path {path}"}, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    return state


def _assert_sent_as_api_key(requests: List[httpx.Request], key: str) -> None:
    assert requests, "the CLI sent no request"
    for request in requests:
        assert request.headers.get("x-api-key") == key, request.url
        assert "authorization" not in request.headers, request.url
        assert "/dgklmnr" not in request.url.path, request.url


class TestApiKeyCredentials:
    def test_layerlens_api_key_is_sent_as_an_api_key(self, runner, clean_env, creds_dir, server) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "key-123")

        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        assert "No traces found." in result.output
        _assert_sent_as_api_key(server.requests, "key-123")
        assert "deprecated" not in _stderr(result)

    def test_api_key_flag_is_sent_as_an_api_key(self, runner, clean_env, creds_dir, server) -> None:
        result = runner.invoke(cli, ["--api-key", "flag-key", "trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        _assert_sent_as_api_key(server.requests, "flag-key")

    @pytest.mark.parametrize("legacy", ["LAYERLENS_STRATIX_API_KEY", "LAYERLENS_ATLAS_API_KEY"])
    def test_legacy_variable_still_works_and_says_what_to_rename(
        self, runner, clean_env, creds_dir, server, legacy: str
    ) -> None:
        clean_env.setenv(legacy, "old-key")

        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        _assert_sent_as_api_key(server.requests, "old-key")
        assert f"warning: {legacy} is deprecated" in _stderr(result)
        assert "LAYERLENS_API_KEY" in _stderr(result)
        # The notice never pollutes stdout, which scripts parse.
        assert "deprecated" not in result.output

    def test_api_key_wins_over_a_stored_login_token(self, runner, clean_env, stored_login, server) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "key-123")

        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        _assert_sent_as_api_key(server.requests, "key-123")

    def test_stored_login_token_is_sent_as_bearer_to_the_login_router(
        self, runner, clean_env, stored_login, server
    ) -> None:
        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        assert server.requests
        for request in server.requests:
            assert request.headers.get("authorization") == "Bearer id-tok"
            assert "x-api-key" not in request.headers
            assert "/dgklmnr/" in request.url.path

    def test_no_credentials_fails_with_every_way_to_fix_it(self, runner, clean_env, creds_dir, server) -> None:
        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 1
        assert "--api-key" in _stderr(result)
        assert "LAYERLENS_API_KEY" in _stderr(result)
        assert "layerlens login" in _stderr(result)
        assert server.requests == []

    def test_legacy_base_url_variable_still_works_and_says_what_to_rename(
        self, runner, clean_env, creds_dir, server
    ) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "key-123")
        clean_env.setenv("LAYERLENS_STRATIX_BASE_URL", "https://staging.example.com/api/v1")

        result = runner.invoke(cli, ["trace", "list"])

        assert result.exit_code == 0, result.output + _stderr(result)
        assert {r.url.host for r in server.requests} == {"staging.example.com"}
        assert "warning: LAYERLENS_STRATIX_BASE_URL is deprecated" in _stderr(result)


class TestWhoami:
    def test_api_key_is_verified_against_the_api(self, runner, clean_env, creds_dir, server) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "key-123")

        result = runner.invoke(cli, ["whoami"])

        assert result.exit_code == 0, result.output + _stderr(result)
        _assert_sent_as_api_key(server.requests, "key-123")
        combined = result.output + _stderr(result)
        assert "LAYERLENS_API_KEY" in combined
        assert "org-1" in combined
        assert "proj-1" in combined

    def test_rejected_api_key_fails(self, runner, clean_env, creds_dir, server) -> None:
        clean_env.setenv("LAYERLENS_API_KEY", "revoked-key")
        server.status = 401

        result = runner.invoke(cli, ["whoami"])

        assert result.exit_code == 1
        assert "Error" in _stderr(result)
        assert "Authenticated" not in result.output + _stderr(result)

    def test_legacy_api_key_variable_is_recognised(self, runner, clean_env, creds_dir, server) -> None:
        clean_env.setenv("LAYERLENS_STRATIX_API_KEY", "old-key")

        result = runner.invoke(cli, ["whoami"])

        assert result.exit_code == 0, result.output + _stderr(result)
        assert "org-1" in result.output + _stderr(result)
        assert "warning: LAYERLENS_STRATIX_API_KEY is deprecated" in _stderr(result)


def _run_entry_point(entry: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ALL_ENV_VARS}
    code = f"import sys; sys.argv = ['prog', *{list(args)!r}]; from layerlens.cli import {entry}; {entry}()"
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)


class TestCommandName:
    def test_version_names_layerlens(self) -> None:
        from layerlens._version import __version__

        proc = _run_entry_point("main", "--version")

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == f"layerlens {__version__}"

    @pytest.mark.parametrize("args", [("--version",), ("--help",), ("trace", "--help")])
    def test_stratix_command_prints_a_notice_and_otherwise_behaves_identically(self, args: tuple) -> None:
        new = _run_entry_point("main", *args)
        old = _run_entry_point("legacy_main", *args)

        assert old.returncode == new.returncode == 0, old.stderr
        assert old.stdout == new.stdout
        assert "warning: the 'stratix' command is deprecated; use 'layerlens' instead" in old.stderr
        assert "deprecated" not in new.stderr

    def test_console_scripts_point_at_the_right_entry_points(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        scripts = pyproject.split("[project.scripts]", 1)[1].split("\n[", 1)[0]

        assert re.search(r'^layerlens = "layerlens\.cli:main"$', scripts, re.M)
        assert re.search(r'^stratix = "layerlens\.cli:legacy_main"$', scripts, re.M)

    def test_banner_no_longer_spells_stratix(self) -> None:
        old_art_first_line = r"  ____ _____ ____      _  _____ _____  __"

        text = banner("9.9.9")

        assert old_art_first_line not in text
        assert "v9.9.9" in text


def _all_command_paths(group: click.Group, prefix: tuple = ()) -> List[tuple]:
    paths: List[tuple] = []
    for name, command in group.commands.items():
        path = (*prefix, name)
        paths.append(path)
        if isinstance(command, click.Group):
            paths.extend(_all_command_paths(command, path))
    return paths


@pytest.mark.parametrize("path", [()] + _all_command_paths(cli), ids=lambda p: " ".join(p) or "root")
def test_help_text_teaches_layerlens(runner, path: tuple) -> None:
    result = runner.invoke(cli, [*path, "--help"])

    assert result.exit_code == 0, result.output
    assert "stratix" not in result.output.lower(), result.output


def test_ci_report_names_layerlens() -> None:
    from layerlens.cli.commands.ci import _build_markdown_report

    report = _build_markdown_report([])

    assert "# LayerLens Evaluation Report" in report
    assert "*Generated by `layerlens ci report`*" in report
    assert "stratix" not in report.lower()
