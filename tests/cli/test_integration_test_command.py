"""`layerlens integration test ID` misread every answer the API gives.

BUG-06 put the route on the API-key router, so the CLI reaches it; tried live on a real Langfuse
integration, the API answered 200 `{"status": "success", "data": {"source_type": "other", "ok": true}}`
(its usual envelope, as the fixtures here answer) and the CLI printed
"Failed to test integration …" (exit 1): TestIntegrationResponse expected `success`. And the API
answers stored credentials that the vendor refuses with a 401 carrying the vendor's reason, which
the CLI printed as "Error: Invalid or missing API key." although the key had just been checked.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
from click.testing import Result

from layerlens import LayerLens
from layerlens.cli._app import cli

from .conftest import _make_runner

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
INTEGRATION = "6af1b12223d101e4ec017a8a"
TEST_PATH = f"/api/v1/organizations/{ORG}/integrations/{INTEGRATION}/test"


def _run(status: int, body: Any, *global_args: str) -> Result:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TEST_PATH and request.method == "POST":
            return httpx.Response(status, json=body)
        return httpx.Response(404, text="404 page not found")

    def client(ctx: Any) -> LayerLens:
        made = LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
        made.organization_id, made.project_id = ORG, PROJECT
        made._transport = httpx.MockTransport(handler)
        return made

    with patch("layerlens.cli.commands.integration.get_client", client):
        return _make_runner().invoke(
            cli, [*global_args, "integration", "test", INTEGRATION], env={"LAYERLENS_API_KEY": "test-key"}
        )


def test_a_passing_test_is_read_as_the_api_answers_it() -> None:
    ok = {"status": "success", "data": {"source_type": "other", "ok": True}}
    result = _run(200, ok)
    assert result.exit_code == 0, result.output
    assert f"Integration {INTEGRATION}: OK" in result.output
    as_json = _run(200, ok, "--format", "json")
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout)["success"] is True


def test_credentials_the_vendor_refused_are_named_not_blamed_on_the_layerlens_key() -> None:
    reason = "langfuse refused the credentials: 401 Unauthorized"
    result = _run(401, {"status": "error", "error": reason})
    assert result.exit_code == 1
    assert reason in result.stderr and "stored credentials" in result.stderr
    assert "Invalid or missing API key" not in result.stderr
