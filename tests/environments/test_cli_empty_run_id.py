"""m5: an empty run id reached the API and came back as "404 page not found".

The docs' CI recipe sets ``RUN_ID=$(layerlens --format json env run list ... | jq -r '.runs[0].run_id')``.
Without pipefail a failed list leaves ``RUN_ID`` empty, and ``env run results/status/wait ENV ""``
exited 1 with ``Error: 404 page not found (HTTP 404)`` (``env run compare`` exited 2 with the same
text), which says nothing about an empty id. Every command that takes a run id now refuses an empty
one as a usage error (exit 2) that names the argument, before it sends anything.
"""

from __future__ import annotations

from typing import Any, List

import httpx
import pytest
from click.testing import CliRunner

from layerlens.cli._app import cli

from ..cli.conftest import _make_runner

ENV = {"LAYERLENS_API_KEY": "k"}


@pytest.fixture
def runner() -> CliRunner:
    return _make_runner()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> List[httpx.Request]:
    requests: List[httpx.Request] = []

    def send(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, text="404 page not found", request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    return requests


def _output(result: Any) -> str:
    try:
        return result.output + (result.stderr or "")
    except (ValueError, AttributeError):
        return result.output


@pytest.mark.parametrize("run_id", ["", "  "])
@pytest.mark.parametrize(
    "argv",
    [
        ["env", "run", "status", "support-desk"],
        ["env", "run", "results", "support-desk"],
        ["env", "run", "wait", "support-desk"],
        ["env", "run", "finish", "support-desk"],
        ["env", "run", "cancel", "support-desk"],
        ["env", "run", "submit", "support-desk", "--attempt", "a1", "--answer", "42"],
    ],
    ids=["status", "results", "wait", "finish", "cancel", "submit"],
)
def test_an_empty_run_id_is_a_usage_error_naming_it(
    runner: CliRunner, sent: List[httpx.Request], argv: List[str], run_id: str
) -> None:
    result = runner.invoke(cli, [*argv, run_id], env=ENV)

    assert result.exit_code == 2, _output(result)
    assert "Invalid value for 'RUN_ID': it is empty" in _output(result)
    assert sent == []


@pytest.mark.parametrize(
    "runs",
    [["--baseline-label", "main", ""], ["", "6afde472cdb2d4ff5d022684"], ["6afde472cdb2d4ff5d022684", ""]],
    ids=["label-and-empty-candidate", "empty-baseline", "empty-candidate"],
)
def test_compare_refuses_an_empty_run_id(runner: CliRunner, sent: List[httpx.Request], runs: List[str]) -> None:
    result = runner.invoke(cli, ["env", "run", "compare", "support-desk", *runs], env=ENV)

    assert result.exit_code == 2, _output(result)
    assert "Invalid value for '[BASELINE] CANDIDATE': a run id is empty" in _output(result)
    assert sent == []


def test_a_run_id_is_still_sent_as_given(runner: CliRunner, sent: List[httpx.Request]) -> None:
    result = runner.invoke(cli, ["env", "run", "status", "support-desk", "6afde472cdb2d4ff5d022684"], env=ENV)

    assert result.exit_code != 2, _output(result)
    assert sent, "the command sent nothing"
