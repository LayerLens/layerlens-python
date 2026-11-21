"""N1: ``layerlens trace list`` showed neither a trace's tags nor its agent.

After ``--tag`` a user could not see why a row matched without ``--format json``, and the agent
the API names for each trace was dropped by the ``Trace`` model before it reached the CLI. The
table now ends with an Agent and a Tags column, and ``Trace.agent`` keeps what the API sends.
"""

from __future__ import annotations

import json
from typing import Any, List
from unittest.mock import patch

import httpx
from click.testing import Result

from layerlens import LayerLens
from layerlens.cli._app import cli

from .conftest import _make_runner

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
LIST_PATH = f"/api/v1/organizations/{ORG}/projects/{PROJECT}/traces"


def _stored(trace_id: str, **fields: Any) -> dict:
    return {
        "id": trace_id,
        "organization_id": ORG,
        "project_id": PROJECT,
        "created_at": "2026-11-07T04:02:29.241Z",
        "filename": f"{trace_id}.json",
        "evaluations_count": 0,
        "last_evaluations": [],
        **fields,
    }


# As the API lists them: an environment attempt's trace, and a trace whose agent it could not name
# (no ``agent`` key, no tags).
TRACES = [
    _stored("6aeea2d5415f6c3a7acc330a", agent="support-agent", tags=["environment", "support-desk", "ci-4512"]),
    _stored("6aeea2daf515e69a5d30bd02"),
]


class _API:
    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == LIST_PATH:
            body = {"traces": TRACES, "count": len(TRACES), "total_count": len(TRACES)}
            return httpx.Response(200, json={"status": "success", "data": body})
        return httpx.Response(404, text="404 page not found")


def _client(api: _API) -> LayerLens:
    made = LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
    made.organization_id, made.project_id = ORG, PROJECT
    made._transport = httpx.MockTransport(api)
    return made


def _invoke(*args: str) -> Result:
    api = _API()
    with patch("layerlens.cli.commands.trace.get_client", lambda ctx: _client(api)):
        return _make_runner().invoke(cli, ["-q", *args], env={"LAYERLENS_API_KEY": "test-key"})


def test_the_sdk_keeps_the_agent_the_api_names() -> None:
    page = _client(_API()).traces.get_many(tags=["support-desk"])
    assert page is not None
    assert [t.agent for t in page.traces] == ["support-agent", None]


def test_trace_list_shows_each_traces_agent_and_tags() -> None:
    result = _invoke("trace", "list", "--tag", "support-desk")
    assert result.exit_code == 0, result.output
    header, _rule, tagged, untagged = result.stdout.splitlines()
    # The new columns come last, so a script that cuts the old ones keeps working.
    assert header.split() == ["ID", "Created", "Filename", "Evaluations", "Agent", "Tags"]
    assert tagged.split()[-4:] == ["support-agent", "environment,", "support-desk,", "ci-4512"]
    assert untagged.split()[-2:] == ["-", "-"]


def test_trace_search_shows_them_too_and_json_keeps_the_list() -> None:
    searched = _invoke("trace", "search", "refund", "--tag", "support-desk")
    assert searched.exit_code == 0, searched.output
    assert searched.stdout.splitlines()[0].split()[-2:] == ["Agent", "Tags"]
    listed = _invoke("--format", "json", "trace", "list")
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.stdout)
    assert [(r["agent"], r["tags"]) for r in rows] == [
        ("support-agent", ["environment", "support-desk", "ci-4512"]),
        (None, []),
    ]
