"""``evaluate run`` and ``bulk eval`` look up ``--model`` / ``--benchmark`` given as an id.

Their help says each option takes "an ID, key, or name", but the resolver tried the id lookup only
for a UUID-shaped value, and LayerLens ids are 24-hex ObjectIds: ``Model not found: 6a0b…``, exit 1
(docs E2E 2026-11-10, L2 B4). The commands run here against an API fake that serves the catalog by
id, as ``GET …/models/{id}`` does, and finds nothing by key or name, as for a catalog model the
project has not added.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import Mock, patch

import httpx
import pytest

from layerlens import LayerLens
from layerlens.cli._app import cli
from layerlens._exceptions import NotFoundError, InternalServerError
from layerlens.cli._client import resolve_model, resolve_benchmark

from .conftest import _make_runner

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
BASE = f"/api/v1/organizations/{ORG}/projects/{PROJECT}"
MODEL = {"id": "6a0b692ced57a8fd01d1433a", "key": "openai/gpt-4o-mini", "name": "GPT-4o mini", "description": ""}
BENCHMARK = {"id": "67e20572e014f9fa6c49b996", "key": "accountingaudit", "name": "Accounting audit"}
UNKNOWN = "6a0b692ced57a8fd01d14fff"


class _Catalog:
    """The API: ids resolve, keys and names find nothing, and an evaluation can be created."""

    def __init__(self) -> None:
        self.created: List[Any] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"{BASE}/models/{MODEL['id']}":
            return httpx.Response(200, json={"status": "success", "data": MODEL})
        if request.method == "GET" and path == f"{BASE}/benchmarks/{BENCHMARK['id']}":
            return httpx.Response(200, json={"status": "success", "data": BENCHMARK})
        if request.method == "GET" and path.startswith((f"{BASE}/models/", f"{BASE}/benchmarks/")):
            kind = "model" if "/models/" in path else "benchmark"
            return httpx.Response(
                404, json={"status": "error", "error": f"{kind} with ID '{path.rsplit('/', 1)[-1]}' not found"}
            )
        if request.method == "GET" and path == f"{BASE}/models":
            return httpx.Response(200, json={"status": "success", "data": {"models": []}})
        if request.method == "GET" and path == f"{BASE}/benchmarks":
            return httpx.Response(200, json={"status": "success", "data": {"datasets": []}})
        if request.method == "POST" and path == f"{BASE}/evaluations":
            self.created.append(json.loads(request.content))
            evaluation = {
                "id": "6b0000000000000000000001",
                "status": "in-progress",
                "submitted_at": 1,
                "finished_at": 0,
                "model_id": MODEL["id"],
                "dataset_id": BENCHMARK["id"],
                "average_duration": 0,
                "accuracy": 0.0,
            }
            return httpx.Response(200, json={"status": "success", "data": [evaluation]})
        return httpx.Response(404, text="404 page not found")


def _client(api: _Catalog) -> LayerLens:
    client = LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(api)
    return client


def test_resolve_model_looks_up_a_24_hex_id() -> None:
    model = resolve_model(_client(_Catalog()), MODEL["id"])
    assert model is not None and (model.id, model.key) == (MODEL["id"], MODEL["key"])


def test_resolve_benchmark_looks_up_a_24_hex_id() -> None:
    benchmark = resolve_benchmark(_client(_Catalog()), BENCHMARK["id"])
    assert benchmark is not None and (benchmark.id, benchmark.key) == (BENCHMARK["id"], BENCHMARK["key"])


def test_an_id_nothing_has_is_looked_up_then_not_found_rather_than_an_error() -> None:
    api = _Catalog()
    seen: List[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return api(request)

    client = _client(api)
    client._transport = httpx.MockTransport(handle)
    assert resolve_model(client, UNKNOWN) is None
    assert resolve_benchmark(client, UNKNOWN) is None
    assert f"{BASE}/models/{UNKNOWN}" in seen and f"{BASE}/benchmarks/{UNKNOWN}" in seen


def test_a_24_hex_key_still_resolves_by_key_when_no_id_matches() -> None:
    client = Mock()
    model = Mock(id="m-1")
    client.models.get_by_id.side_effect = NotFoundError(
        "model not found",
        response=httpx.Response(404, request=httpx.Request("GET", "https://api.test")),
        body=None,
    )
    client.models.get_by_key.return_value = model
    assert resolve_model(client, "abcdefabcdefabcdefabcdef") is model
    client.models.get_by_id.assert_called_once_with("abcdefabcdefabcdefabcdef")
    client.models.get_by_key.assert_called_once_with("abcdefabcdefabcdefabcdef")


def test_a_24_hex_benchmark_key_still_resolves_by_key_when_no_id_matches() -> None:
    client = Mock()
    benchmark = Mock(id="b-1")
    client.benchmarks.get_by_id.side_effect = NotFoundError(
        "benchmark not found",
        response=httpx.Response(404, request=httpx.Request("GET", "https://api.test")),
        body=None,
    )
    client.benchmarks.get_by_key.return_value = benchmark
    assert resolve_benchmark(client, "abcdefabcdefabcdefabcdef") is benchmark
    client.benchmarks.get_by_id.assert_called_once_with("abcdefabcdefabcdefabcdef")
    client.benchmarks.get_by_key.assert_called_once_with("abcdefabcdefabcdefabcdef")


def test_an_id_lookup_that_fails_other_than_not_found_is_an_error_not_a_miss() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{BASE}/models/{MODEL['id']}":
            return httpx.Response(500, json={"status": "error", "error": "the model catalogue could not be read"})
        return _Catalog()(request)

    client = _client(_Catalog())
    client._transport = httpx.MockTransport(broken)
    with pytest.raises(InternalServerError):
        resolve_model(client, MODEL["id"])


@pytest.mark.parametrize(
    "argv",
    [
        ["evaluate", "run", "--model", MODEL["id"], "--benchmark", BENCHMARK["id"]],
        ["bulk", "eval", "--model", MODEL["id"], "--benchmark", BENCHMARK["id"]],
    ],
    ids=["evaluate-run", "bulk-eval"],
)
def test_the_commands_create_the_evaluation_for_ids(argv: List[str]) -> None:
    api = _Catalog()
    target = (
        "layerlens.cli.commands.evaluate.get_client"
        if argv[0] == "evaluate"
        else "layerlens.cli.commands.bulk.get_client"
    )
    with patch(target, return_value=_client(api)):
        result = _make_runner().invoke(cli, argv, env={"LAYERLENS_API_KEY": "test"})
    assert result.exit_code == 0, result.output
    expected: Dict[str, Any] = {"model_id": MODEL["id"], "dataset_id": BENCHMARK["id"]}
    assert [{k: body[0][k] for k in expected} for body in api.created] == [expected]
