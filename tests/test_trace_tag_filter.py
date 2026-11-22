"""Filtering traces by tag: ``traces.get_many(tags=...)`` and ``layerlens trace list --tag`` (LAY-4430).

The clients are real; only their transport is an ``httpx.MockTransport``, so the query
string asserted here is the one the API receives: one ``tag`` value per tag.
"""

from __future__ import annotations

import json
import asyncio
from typing import Any, Dict, List, Iterator
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from layerlens import LayerLens, AsyncLayerLens
from layerlens.models import Project, Organization
from layerlens.cli._app import cli

from .cli.conftest import _make_runner

ORG = "691254ecd44a29f6926881a2"
PROJECT = "691254eed44a29f6926881a3"
TRACES = f"/api/v1/organizations/{ORG}/projects/{PROJECT}/traces"
_ORGANIZATION = Organization(id=ORG, name="Org", projects=[Project(id=PROJECT, name="Starter")])

TRACE = {
    "id": "6a91c0f2a7b8c9d0e1f2a3b4",
    "organization_id": ORG,
    "project_id": PROJECT,
    "created_at": "2026-11-04T10:00:00Z",
    "filename": "simulated-6a91c0e5f1d2a3b4c5d6e7f8-0001.json",
    "tags": ["environment", "support-desk", "3f2c1ab"],
    "evaluations_count": 0,
}


class _Recorder:
    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path != TRACES:
            return httpx.Response(404, text="404 page not found")
        body: Dict[str, Any] = {"status": "success", "data": {"traces": [TRACE], "count": 1, "total_count": 1}}
        return httpx.Response(200, json=body)


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def client(recorder: _Recorder) -> LayerLens:
    client = LayerLens(api_key="test-key", base_url="https://api.test/api/v1")
    client.organization_id, client.project_id = _ORGANIZATION.id, _ORGANIZATION.projects[0].id
    client._transport = httpx.MockTransport(recorder.handle)
    return client


@pytest.fixture
def async_client(recorder: _Recorder) -> Iterator[AsyncLayerLens]:
    client = AsyncLayerLens(api_key="test-key", base_url="https://api.test/api/v1")
    client.organization_id, client.project_id = _ORGANIZATION.id, _ORGANIZATION.projects[0].id
    client._transport = httpx.MockTransport(recorder.handle)
    yield client


def _tags_sent(recorder: _Recorder) -> List[str]:
    (request,) = recorder.requests
    return request.url.params.get_list("tag")


class TestSync:
    def test_each_tag_is_its_own_query_value(self, client: LayerLens, recorder: _Recorder) -> None:
        client.traces.get_many(tags=["environment", "support-desk", "3f2c1ab"])
        assert _tags_sent(recorder) == ["environment", "support-desk", "3f2c1ab"]

    def test_a_tag_with_a_comma_is_sent_whole(self, client: LayerLens, recorder: _Recorder) -> None:
        client.traces.get_many(tags=["release, 2026-11"])
        assert _tags_sent(recorder) == ["release, 2026-11"]

    def test_no_tags_sends_no_tag_filter(self, client: LayerLens, recorder: _Recorder) -> None:
        client.traces.get_many()
        assert _tags_sent(recorder) == []

    def test_the_traces_tags_are_read(self, client: LayerLens) -> None:
        result = client.traces.get_many(tags=["environment"])
        assert result is not None
        assert result.traces[0].tags == ["environment", "support-desk", "3f2c1ab"]

    def test_one_string_is_refused_rather_than_read_as_its_letters(
        self, client: LayerLens, recorder: _Recorder
    ) -> None:
        with pytest.raises(TypeError, match="list of tags"):
            client.traces.get_many(tags="environment")  # type: ignore[arg-type]
        assert recorder.requests == []

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_tag_is_refused_since_the_api_would_drop_the_filter(
        self, client: LayerLens, recorder: _Recorder, blank: str
    ) -> None:
        # The API ignores a blank tag: sent, it would list every trace in the project.
        with pytest.raises(ValueError, match="empty"):
            client.traces.get_many(tags=["environment", blank])
        assert recorder.requests == []


class TestAsync:
    def test_each_tag_is_its_own_query_value(self, async_client: AsyncLayerLens, recorder: _Recorder) -> None:
        asyncio.run(async_client.traces.get_many(tags=["environment", "support-desk"]))
        assert _tags_sent(recorder) == ["environment", "support-desk"]

    def test_one_string_is_refused(self, async_client: AsyncLayerLens, recorder: _Recorder) -> None:
        with pytest.raises(TypeError, match="list of tags"):
            asyncio.run(async_client.traces.get_many(tags="environment"))  # type: ignore[arg-type]
        assert recorder.requests == []

    def test_a_blank_tag_is_refused(self, async_client: AsyncLayerLens, recorder: _Recorder) -> None:
        with pytest.raises(ValueError, match="empty"):
            asyncio.run(async_client.traces.get_many(tags=[""]))
        assert recorder.requests == []


class TestCli:
    @pytest.fixture
    def runner(self) -> CliRunner:
        return _make_runner()

    @pytest.fixture(autouse=True)
    def _cli_on_the_fake_api(self, client: LayerLens) -> Iterator[None]:
        with patch("layerlens.cli.commands.trace.get_client", lambda ctx: client):
            yield

    def _invoke(self, runner: CliRunner, *args: str) -> Any:
        return runner.invoke(cli, list(args), env={"LAYERLENS_API_KEY": "test-key"}, catch_exceptions=False)

    def test_list_takes_repeated_tags(self, runner: CliRunner, recorder: _Recorder) -> None:
        result = self._invoke(runner, "trace", "list", "--tag", "environment", "--tag", "3f2c1ab")
        assert result.exit_code == 0, result.stderr
        assert _tags_sent(recorder) == ["environment", "3f2c1ab"]
        assert TRACE["id"] in result.stdout

    def test_search_takes_tags_too(self, runner: CliRunner, recorder: _Recorder) -> None:
        result = self._invoke(runner, "trace", "search", "refund", "--tag", "support-desk")
        assert result.exit_code == 0, result.stderr
        (request,) = recorder.requests
        assert request.url.params.get_list("tag") == ["support-desk"]
        assert request.url.params["search"] == "refund"

    def test_json_output_shows_each_traces_tags(self, runner: CliRunner) -> None:
        result = self._invoke(runner, "--format", "json", "trace", "list", "--tag", "environment")
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)[0]["tags"] == ["environment", "support-desk", "3f2c1ab"]

    @pytest.mark.parametrize("command", [("trace", "list"), ("trace", "search", "refund")])
    def test_a_blank_tag_is_a_usage_error_and_nothing_is_listed(
        self, runner: CliRunner, recorder: _Recorder, command: tuple
    ) -> None:
        # `--tag "$GIT_SHA"` with the variable unset: the API would ignore the blank tag
        # and list every trace in the project as if they were the run's.
        result = self._invoke(runner, *command, "--tag", "environment", "--tag", "")
        assert result.exit_code == 2
        assert "--tag" in result.stderr and "empty" in result.stderr
        assert recorder.requests == []
