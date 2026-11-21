"""m7, the CLI half: a 200 without an ``evaluations`` array makes ``evaluate list`` and ``ci report`` exit 1.

Before, they printed "No evaluations found." / an empty report and exited 0. The SDK half is in
``tests/resources/test_evaluations_list_shape.py``.
"""

from __future__ import annotations

import inspect
from typing import Any, Iterator
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from layerlens import LayerLens
from layerlens.cli._app import cli

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"


def _client(body: Any) -> LayerLens:
    client = LayerLens(api_key="k", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    return client


@pytest.fixture
def runner() -> CliRunner:
    # stderr apart from stdout, on click before and after 8.3 (as tests/cli/conftest.py does)
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)  # type: ignore[call-arg]
    return CliRunner()


@pytest.fixture
def unreadable_api() -> Iterator[LayerLens]:
    client = _client({"total_count": 3})
    with patch("layerlens.cli.commands.evaluate.get_client", return_value=client), patch(
        "layerlens.cli.commands.ci.get_client", return_value=client
    ):
        yield client


@pytest.mark.parametrize("argv", [["evaluate", "list"], ["--format", "json", "evaluate", "list"]])
def test_evaluate_list_exits_1(runner: CliRunner, unreadable_api: LayerLens, argv: Any) -> None:
    result = runner.invoke(cli, argv, env={"LAYERLENS_API_KEY": "k"})

    assert result.exit_code == 1, result.output
    assert "no `evaluations` array" in result.stderr
    assert "No evaluations found" not in result.output + result.stderr
    assert result.stdout == ""


def test_ci_report_latest_exits_1(runner: CliRunner, unreadable_api: LayerLens) -> None:
    result = runner.invoke(cli, ["ci", "report", "--latest", "1"], env={"LAYERLENS_API_KEY": "k"})

    assert result.exit_code == 1, result.output
    assert "no `evaluations` array" in result.stderr
    assert "No benchmark evaluations" not in result.stdout
