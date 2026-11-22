"""BUG-05: ``scorer create`` reported failure after creating the scorer.

The API answers ``POST /scorers`` with ``{"data": {"id": "..."}}``. ``create()`` built
``Scorer(**data)`` from it, which needs ``name``; the validation error was swallowed into ``None``
and ``layerlens scorer create`` printed ``Failed to create scorer.`` and exited 1, for a scorer that
existed. A refused create also printed the raw exception (``Error: Error code: 400 - {...}``).
"""

from __future__ import annotations

import json
import asyncio
from typing import Any, Dict, List, Tuple, Callable
from unittest.mock import patch

import httpx
import pytest

from layerlens import LayerLens, AsyncLayerLens
from layerlens.cli._app import cli
from layerlens._exceptions import LayerLensError

from .cli.conftest import _make_runner

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
SCORERS = f"/api/v1/organizations/{ORG}/projects/{PROJECT}/scorers"
NEW_ID = "6aee6972ef77db5e918762ce"
REQUEST = {
    "name": "one-word",
    "description": "Scores whether the reply is one word.",
    "model_id": "6a5f9bd2d357a8f80ad03749",
    "prompt": "Score 1 if the reply is one word, else 0.",
}
STORED = {
    **REQUEST,
    "id": NEW_ID,
    "organization_id": ORG,
    "project_id": PROJECT,
    "model_name": "Gemini 3.5 Flash-Lite",
    "created_at": "2026-11-06T20:39:00Z",
}

Route = Callable[[httpx.Request], httpx.Response]


def _api(get_scorer: Route) -> Tuple[Route, List[httpx.Request]]:
    """The API as it answers: the create with the new id only, the read with the whole scorer."""
    seen: List[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.path == SCORERS:
            return httpx.Response(200, json={"status": "success", "data": {"id": NEW_ID}})
        if request.method == "GET" and request.url.path == f"{SCORERS}/{NEW_ID}":
            return get_scorer(request)
        return httpx.Response(404, text="404 page not found")

    return handle, seen


def _stored(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": STORED})


def _gone(_: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"status": "error", "error": "scorer not found"})


def _client(handler: Route) -> LayerLens:
    client = LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(handler)
    return client


def _async_client(handler: Route) -> AsyncLayerLens:
    client = AsyncLayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    client.organization_id, client.project_id = ORG, PROJECT
    client._transport = httpx.MockTransport(handler)
    return client


class TestCreateReturnsTheScorerItCreated:
    def test_the_scorer_is_read_back_by_the_id_the_api_gave(self) -> None:
        handler, seen = _api(_stored)
        scorer = _client(handler).scorers.create(**REQUEST)
        assert scorer.id == NEW_ID and scorer.name == "one-word" and scorer.model_name == "Gemini 3.5 Flash-Lite"
        assert [(r.method, r.url.path) for r in seen] == [("POST", SCORERS), ("GET", f"{SCORERS}/{NEW_ID}")]
        assert json.loads(seen[0].content) == REQUEST

    def test_a_scorer_that_cannot_be_read_back_is_still_returned_with_its_id(self) -> None:
        handler, _ = _api(_gone)
        scorer = _client(handler).scorers.create(**REQUEST)
        assert scorer.id == NEW_ID and scorer.name == "one-word" and scorer.prompt == REQUEST["prompt"]

    def test_a_reply_without_the_new_scorer_raises_instead_of_returning_none(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "data": {"created": True}})

        with pytest.raises(LayerLensError, match="without the new scorer"):
            _client(handle).scorers.create(**REQUEST)

    def test_a_whole_reply_that_does_not_validate_still_returns_the_scorer_by_its_id(self) -> None:
        # The scorer exists: a reply of an unexpected shape must not lose it (CodeRabbit on #99).
        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"status": "success", "data": {"id": NEW_ID, "name": None}})
            return _stored(request)

        scorer = _client(handle).scorers.create(**REQUEST)
        assert scorer.id == NEW_ID and scorer.name == "one-word"

    def test_the_async_client_returns_the_scorer_it_created(self) -> None:
        handler, seen = _api(_stored)
        scorer = asyncio.run(_async_client(handler).scorers.create(**REQUEST))
        assert scorer.id == NEW_ID and scorer.name == "one-word"
        assert [(r.method, r.url.path) for r in seen] == [("POST", SCORERS), ("GET", f"{SCORERS}/{NEW_ID}")]

    def test_the_async_client_raises_on_a_reply_without_the_new_scorer(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "success", "data": {"created": True}})

        with pytest.raises(LayerLensError, match="without the new scorer"):
            asyncio.run(_async_client(handle).scorers.create(**REQUEST))


class TestTheScorerCreateCommand:
    def _invoke(self, handler: Route, *extra: str) -> Any:
        args: List[str] = [*extra, "scorer", "create"]
        for key, flag in (
            ("name", "--name"),
            ("description", "--description"),
            ("model_id", "--model-id"),
            ("prompt", "--prompt"),
        ):
            args += [flag, REQUEST[key]]
        with patch("layerlens.cli.commands.scorer.get_client", lambda ctx: _client(handler)):
            return _make_runner().invoke(cli, ["-q", *args], env={"LAYERLENS_API_KEY": "test-key"})

    def test_prints_the_scorer_it_created(self) -> None:
        handler, _ = _api(_stored)
        result = self._invoke(handler, "--format", "json")
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)["id"] == NEW_ID
        assert "Failed to create scorer" not in result.stderr

    def test_a_refused_create_says_why_in_the_apis_words(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            body: Dict[str, Any] = {"status": "error", "error": "scorer name 'one-word' is already taken"}
            return httpx.Response(400, json=body)

        result = self._invoke(refuse)
        assert result.exit_code == 1
        assert "Error: scorer name 'one-word' is already taken (HTTP 400)" in result.stderr
        assert "Error code:" not in result.stderr
