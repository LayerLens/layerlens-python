"""m6: ``evaluate get`` never printed the API's sentence, and gave the trace-evaluation hint for any error.

For every refused request (400, 403, 404, 409, 422, 500, 503) it printed "could not fetch evaluation
<id> (HTTP n)" and "If this ID came from `layerlens judge test`, it is a trace evaluation": the
API's own sentence (a Standard key's 403 says what to do) was dropped, and a 403 or a 503 got a hint
about trace evaluations. Every other command prints the API's sentence. The API answers 404 for an
id no evaluation has, a trace evaluation's included, so the hint now comes with a 404 only.
"""

from __future__ import annotations

from typing import Any, Iterator
from unittest.mock import Mock, patch

import httpx
import pytest
from click.testing import CliRunner

from layerlens.cli._app import cli
from layerlens._exceptions import (
    ConflictError,
    NotFoundError,
    BadRequestError,
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
    UnprocessableEntityError,
)

ID = "ffffffffffffffffffffffff"
HINT = f"use `layerlens judge result {ID}` instead"
ENV = {"LAYERLENS_API_KEY": "test"}


def _refused(cls: Any, status: int, sentence: str) -> Any:
    request = httpx.Request("GET", f"https://api.test/api/v1/evaluations/{ID}")
    response = httpx.Response(status, json={"status": "error", "error": sentence}, request=request)
    return cls(f"Error code: {status}", response=response, body=sentence)


@pytest.fixture
def client() -> Iterator[Mock]:
    client = Mock()
    with patch("layerlens.cli.commands.evaluate.get_client", return_value=client):
        yield client


@pytest.mark.parametrize(
    ("cls", "status", "sentence"),
    [
        (BadRequestError, 400, "evaluation ID 'x' is not valid"),
        (PermissionDeniedError, 403, "this key has Standard access; use an Automation key"),
        (ConflictError, 409, "the evaluation is being updated"),
        (UnprocessableEntityError, 422, "the request could not be read"),
        (InternalServerError, 500, "Internal server error"),
        (InternalServerError, 503, "the service is unavailable — try again in a moment"),
    ],
)
def test_an_error_prints_the_apis_sentence_without_the_hint(
    runner: CliRunner, client: Mock, cls: Any, status: int, sentence: str
) -> None:
    client.evaluations.get_by_id.side_effect = _refused(cls, status, sentence)

    result = runner.invoke(cli, ["evaluate", "get", ID], env=ENV)

    assert result.exit_code == 1
    assert result.stderr == f"Error: {sentence} (HTTP {status})\n"
    assert result.stdout == ""


def test_a_404_prints_the_apis_sentence_and_the_hint(runner: CliRunner, client: Mock) -> None:
    client.evaluations.get_by_id.side_effect = _refused(NotFoundError, 404, "evaluation not found")

    result = runner.invoke(cli, ["evaluate", "get", ID], env=ENV)

    assert result.exit_code == 1
    assert result.stderr.splitlines()[0] == "Error: evaluation not found (HTTP 404)"
    assert HINT in result.stderr


def test_a_refused_key_says_so(runner: CliRunner, client: Mock) -> None:
    client.evaluations.get_by_id.side_effect = _refused(AuthenticationError, 401, "invalid API key")

    result = runner.invoke(cli, ["evaluate", "get", ID], env=ENV)

    assert result.exit_code == 1
    assert "Invalid or missing API key" in result.stderr
    assert HINT not in result.stderr
