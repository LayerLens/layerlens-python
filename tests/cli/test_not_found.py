"""A 404 prints the API's own sentence, as every other refusal does (R4 F8).

The CLI caught NotFoundError before APIStatusError and printed
`Error: Resource not found. Error code: 404 - {'status': 'error', 'error': "trace with ID '…' not found"}`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest
from click.testing import Result

from layerlens import LayerLens
from layerlens.cli._app import cli

from .conftest import _make_runner

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
MISSING = "000000000000000000000000"
SENTENCE = f"trace with ID '{MISSING}' not found"


def _get_trace(*global_args: str) -> Result:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/organizations/{ORG}/projects/{PROJECT}/traces/{MISSING}":
            return httpx.Response(404, json={"status": "error", "error": SENTENCE})
        return httpx.Response(404, text="404 page not found")

    def client(ctx: Any) -> LayerLens:
        made = LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
        made.organization_id, made.project_id = ORG, PROJECT
        made._transport = httpx.MockTransport(handler)
        return made

    with patch("layerlens.cli.commands.trace.get_client", client):
        return _make_runner().invoke(
            cli, [*global_args, "trace", "get", MISSING], env={"LAYERLENS_API_KEY": "test-key"}
        )


@pytest.mark.parametrize("global_args", [(), ("--format", "json")], ids=["table", "json"])
def test_a_missing_trace_prints_the_apis_sentence(global_args: tuple) -> None:
    result = _get_trace(*global_args)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == f"Error: {SENTENCE} (HTTP 404)"
