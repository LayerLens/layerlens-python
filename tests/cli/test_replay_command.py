"""BUG-07: ``layerlens replay run --trace-id ID`` never loaded that trace.

Without ``--trace-file`` it replayed an empty local trace and printed ``"status": "completed"``,
also for an id that names no trace; a ``--replay-fn`` that raised printed ``"status": "failed"``
and exited 0. It now reads the stored trace through the API, refuses an id with no trace as a
usage error, and exits 1 when the replay does not complete.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any, Dict, List
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from click.testing import Result

from layerlens import LayerLens
from layerlens.cli._app import cli

from .conftest import _make_runner

ORG, PROJECT = "691254ecd44a29f6926881a2", "691254eed44a29f6926881a3"
TRACE_ID = "6aee8ab0f515e69a5d2cc4e9"
TRACE_PATH = f"/api/v1/organizations/{ORG}/projects/{PROJECT}/traces/{TRACE_ID}"
EVENTS = [{"event_type": "tool.call", "sequence_id": i, "payload": {"tool_name": f"t{i}"}} for i in range(1, 6)]
STORED = {
    "id": TRACE_ID,
    "organization_id": ORG,
    "project_id": PROJECT,
    "created_at": "2026-11-06T23:17:00Z",
    "filename": "trace.json",
    "data": {"trace_id": "3ca9fa414eb8409f", "events": EVENTS},
}

# Records what the replay was given, so a test can see which trace it replayed.
RECORDER = """
import json, os
from layerlens.models.trace import Trace

def replay(trace, request):
    with open(os.environ["REPLAY_SEEN"], "w") as f:
        json.dump({"id": trace.id, "events": len((trace.data or {}).get("events") or [])}, f)
    return Trace(**{**trace.model_dump(), "id": "replayed-" + trace.id})

def boom(trace, request):
    raise RuntimeError("the agent under replay crashed")
"""


@pytest.fixture
def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "replay_recorder.py").write_text(textwrap.dedent(RECORDER))
    monkeypatch.syspath_prepend(str(tmp_path))
    seen = tmp_path / "seen.json"
    monkeypatch.setenv("REPLAY_SEEN", str(seen))
    return seen


class _API:
    def __init__(self, status: int, body: Any) -> None:
        self.status, self.body = status, body
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.startswith(f"/api/v1/organizations/{ORG}/projects/{PROJECT}/traces/"):
            return httpx.Response(self.status, json=self.body)
        return httpx.Response(404, text="404 page not found")


def _invoke(api: _API, *args: str) -> Result:
    def client(ctx: Any) -> LayerLens:
        made = LayerLens(api_key="test-key", base_url="https://api.test/api/v1", max_retries=0)
        made.organization_id, made.project_id = ORG, PROJECT
        made._transport = httpx.MockTransport(api)
        return made

    # create=True: the command before BUG-07's fix never built a client, so it had no get_client.
    with patch("layerlens.cli.commands.replay.get_client", client, create=True):
        return _make_runner().invoke(cli, ["-q", "replay", "run", *args], env={"LAYERLENS_API_KEY": "test-key"})


def test_replays_the_stored_trace_the_id_names(recorder: Path) -> None:
    api = _API(200, {"status": "success", "data": STORED})
    result = _invoke(api, "--trace-id", TRACE_ID, "--replay-fn", "replay_recorder:replay")
    assert result.exit_code == 0, result.stderr
    assert json.loads(recorder.read_text()) == {"id": TRACE_ID, "events": 5}
    assert [r.url.path for r in api.requests] == [TRACE_PATH]
    assert json.loads(result.stdout)["status"] == "completed"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, {"status": "error", "error": f"trace with ID '{TRACE_ID}' not found"}),
        (400, {"status": "error", "error": "invalid trace ID format"}),
    ],
)
def test_an_id_that_names_no_stored_trace_is_a_usage_error(status: int, body: Dict[str, Any], recorder: Path) -> None:
    result = _invoke(_API(status, body), "--trace-id", TRACE_ID, "--replay-fn", "replay_recorder:replay")
    assert result.exit_code == 2, result.output
    assert TRACE_ID in result.stderr and body["error"] in result.stderr
    assert not recorder.exists(), "a replay ran on a trace that does not exist"
    assert '"status"' not in result.stdout


def test_a_trace_file_of_another_trace_is_named(recorder: Path, tmp_path: Path) -> None:
    # The file holds another trace than --trace-id: the replay and its result are the file's
    # trace, and the warning says so (CodeRabbit on #99).
    exported = tmp_path / "trace.json"
    exported.write_text(json.dumps(STORED))
    other = "6aee000000000000000000aa"
    result = _invoke(
        _API(500, {}), "--trace-id", other, "--trace-file", str(exported), "--replay-fn", "replay_recorder:replay"
    )
    assert result.exit_code == 0, result.stderr
    assert f"Warning: --trace-file holds trace {TRACE_ID}, not {other}; the result names {TRACE_ID}.\n" in result.stderr
    assert json.loads(result.stdout)["original_trace_id"] == TRACE_ID
    assert json.loads(recorder.read_text())["id"] == TRACE_ID


def test_a_replay_that_fails_exits_1_and_a_trace_file_needs_no_api(recorder: Path, tmp_path: Path) -> None:
    exported = tmp_path / "trace.json"
    exported.write_text(json.dumps(STORED))
    api = _API(500, {"status": "error", "error": "must not be called"})
    result = _invoke(api, "--trace-id", TRACE_ID, "--trace-file", str(exported), "--replay-fn", "replay_recorder:boom")
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["status"] == "failed"
    assert "the agent under replay crashed" in result.stderr
    assert api.requests == []
