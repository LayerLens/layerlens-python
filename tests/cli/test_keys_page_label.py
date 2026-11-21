"""The SDK names the keys page as the app does: "Manage organization → API keys".

The SDK sent users to "(Organization Settings → API Keys)", a page the app does not have; the app's
path, and the API's refusals since layerlens-app#2910, say "(Manage organization → API keys)" (docs
re-run 2026-11-10). The CLI's `env` help and its no-key error, and client.environments' refusal of a
login session, now say it, and no source file keeps the old label.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import layerlens
from layerlens import LayerLens
from layerlens.cli._app import cli
from layerlens._exceptions import LayerLensError

PAGE = "(Manage organization → API keys)"
OLD = "Organization Settings"


def test_env_help_names_the_keys_page() -> None:
    result = CliRunner().invoke(cli, ["env", "--help"])
    assert result.exit_code == 0
    assert PAGE in " ".join(result.output.split())
    assert OLD not in result.output


def test_env_without_a_key_names_the_keys_page() -> None:
    result = CliRunner().invoke(cli, ["-q", "env", "list"], env={"LAYERLENS_API_KEY": ""})
    assert result.exit_code == 1
    assert PAGE in result.output and OLD not in result.output


def test_environments_refuse_a_login_session_naming_the_keys_page() -> None:
    client = LayerLens(api_key="session-token", base_url="https://api.test/api/v1", use_bearer_auth=True)
    with pytest.raises(LayerLensError, match="needs an API key with Automation access") as refused:
        client.environments  # noqa: B018 — the refusal is raised on first use
    assert PAGE in str(refused.value) and OLD not in str(refused.value)


def test_no_source_file_keeps_the_old_label() -> None:
    package = Path(layerlens.__file__).parent
    stale = [str(p.relative_to(package)) for p in package.rglob("*.py") if OLD in p.read_text(encoding="utf-8")]
    assert stale == []
