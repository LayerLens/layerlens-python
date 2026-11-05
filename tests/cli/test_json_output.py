"""With ``--format json``, stdout carries only JSON (LAY-4250).

``layerlens --format json judge test ...`` printed ``Trace evaluation created: <id>``
before the JSON, and ``evaluate run --wait`` printed its progress the same way, so
piping either to ``jq`` failed. Under ``--format json`` every human-readable line
now goes to stderr: a command that returns data prints exactly one JSON document
on stdout (``[]`` for an empty list), and one that returns none prints nothing.
The table format is unchanged.

The SDK client is replaced at the command module (the layer that talks to the
API) and returns real model objects, so the formatter runs for real.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest
from click.testing import CliRunner

from layerlens.models import (
    Judge,
    Model,
    Trace,
    Scorer,
    Benchmark,
    Evaluation,
    TraceEvaluation,
    EvaluationStatus,
    TraceEvaluationStatus,
    api as api_models,
)
from layerlens.cli._app import cli

from .conftest import _make_runner

MODEL = Model(id="m-1", key="gpt-4", name="GPT-4", description="")
BENCHMARK = Benchmark(id="b-1", key="mmlu", name="MMLU")
JUDGE = Judge(
    id="j-1",
    organization_id="org-1",
    project_id="proj-1",
    name="Quality",
    evaluation_goal="Evaluate response quality",
    version=1,
    created_at="2026-10-14T00:00:00Z",
    updated_at="2026-10-14T00:00:00Z",
)
TRACE_EVALUATION = TraceEvaluation(id="te-1", trace_id="tr-1", judge_id="j-1", status=TraceEvaluationStatus.PENDING)
SCORER = Scorer(id="s-1", name="Quality", description="Evaluate quality", model_id="m-1", prompt="Rate it")
TRACE = Trace(
    id="tr-1", organization_id="org-1", project_id="proj-1", created_at="2026-10-14T00:00:00Z", filename="run.jsonl"
)


def _evaluation(status: str) -> Evaluation:
    return Evaluation(
        id="ev-1",
        status=EvaluationStatus(status),
        submitted_at=1760400000,
        finished_at=0,
        model_id="m-1",
        model_name="GPT-4",
        dataset_id="b-1",
        dataset_name="MMLU",
        average_duration=0,
        accuracy=0.0,
    )


def _client() -> Mock:
    client = Mock()
    client.models.get_by_key.return_value = MODEL
    client.benchmarks.get_by_key.return_value = BENCHMARK
    client.judges.create.return_value = JUDGE
    client.trace_evaluations.create.return_value = TRACE_EVALUATION
    client.evaluations.create.return_value = _evaluation("pending")
    client.evaluations.wait_for_completion.return_value = _evaluation("success")
    client.evaluations.get_many.return_value = SimpleNamespace(evaluations=[_evaluation("success")])
    client.scorers.create.return_value = SCORER
    client.scorers.delete.return_value = True
    client.integrations.test.return_value = api_models.TestIntegrationResponse(success=True, message="Connected")
    client.traces.get.return_value = TRACE
    client.traces.delete.return_value = True
    return client


def _stderr(result: Any) -> str:
    try:
        return result.stderr or ""
    except (ValueError, AttributeError):
        return ""


@pytest.fixture
def runner() -> CliRunner:
    return _make_runner()


@pytest.fixture
def files(tmp_path: Path) -> Dict[str, str]:
    jobs = tmp_path / "jobs.jsonl"
    jobs.write_text('{"model": "gpt-4", "benchmark": "mmlu"}\n')
    traces = tmp_path / "trace_ids.txt"
    traces.write_text("tr-1\n")
    return {"jobs": str(jobs), "traces": str(traces), "out": str(tmp_path / "out.json")}


def _invoke(runner: Any, module: str, argv: List[str], client: Optional[Mock] = None) -> Any:
    with patch(f"layerlens.cli.commands.{module}.get_client", return_value=client or _client()):
        return runner.invoke(cli, argv, env={"LAYERLENS_API_KEY": "k"})


# (module, argv after `--format json`, the JSON stdout must hold, lines stderr must hold)
RETURNS_DATA = [
    ("judge", ["judge", "create", "--name", "Quality", "--goal", "Evaluate"], {"id": "j-1"}, ["Judge created: j-1"]),
    (
        "judge",
        ["judge", "test", "--judge-id", "j-1", "--trace-id", "tr-1"],
        {"id": "te-1"},
        ["Trace evaluation created: te-1", "layerlens judge result te-1"],
    ),
    (
        "evaluate",
        ["evaluate", "run", "--model", "gpt-4", "--benchmark", "mmlu", "--wait"],
        {"id": "ev-1", "status": "success"},
        [
            "Creating evaluation: GPT-4 x MMLU",
            "Evaluation created: ev-1 (status: pending)",
            "Waiting for completion",
            "Evaluation finished: success",
        ],
    ),
    (
        "scorer",
        [
            "scorer",
            "create",
            "--name",
            "Quality",
            "--description",
            "Evaluate quality",
            "--model-id",
            "m-1",
            "--prompt",
            "Rate it",
        ],
        {"id": "s-1"},
        ["Scorer created: s-1"],
    ),  # fmt: skip
    (
        "integration",
        ["integration", "test", "i-1"],
        {"success": True, "message": "Connected"},
        ["Integration i-1: OK", "Message: Connected"],
    ),
    (
        "bulk",
        ["bulk", "eval", "--model", "gpt-4", "--benchmark", "mmlu", "--wait"],
        {"id": "ev-1", "status": "success"},
        [
            "Creating evaluation: GPT-4 x MMLU",
            "Evaluation created: ev-1",
            "Waiting for completion",
            "Evaluation finished: success",
        ],
    ),
]


@pytest.mark.parametrize(
    "module, argv, expected, status_lines", RETURNS_DATA, ids=[" ".join(c[1][:2]) for c in RETURNS_DATA]
)
def test_status_lines_go_to_stderr_and_stdout_is_the_json(
    runner: CliRunner, module: str, argv: List[str], expected: Dict[str, Any], status_lines: List[str]
) -> None:
    result = _invoke(runner, module, ["--format", "json", *argv])

    assert result.exit_code == 0, result.output + _stderr(result)
    document = json.loads(result.stdout)
    assert {k: document[k] for k in expected} == expected
    for line in status_lines:
        assert line in _stderr(result)


def test_bulk_eval_from_a_file_prints_the_created_evaluations(runner: CliRunner, files: Dict[str, str]) -> None:
    result = _invoke(runner, "bulk", ["--format", "json", "bulk", "eval", "--file", files["jobs"]])

    assert result.exit_code == 0, result.output + _stderr(result)
    assert [(e["id"], e["status"]) for e in json.loads(result.stdout)] == [("ev-1", "pending")]
    assert "[1] Created: ev-1" in _stderr(result)


def test_bulk_eval_from_a_file_with_wait_prints_the_finished_evaluations(
    runner: CliRunner, files: Dict[str, str]
) -> None:
    result = _invoke(runner, "bulk", ["--format", "json", "bulk", "eval", "--file", files["jobs"], "--wait"])

    assert result.exit_code == 0, result.output + _stderr(result)
    assert [(e["id"], e["status"]) for e in json.loads(result.stdout)] == [("ev-1", "success")]
    assert "  ev-1: success" in _stderr(result)


def test_bulk_eval_from_a_file_reports_an_evaluation_that_disappears_while_waiting(
    runner: CliRunner, files: Dict[str, str]
) -> None:
    client = _client()
    client.evaluations.wait_for_completion.return_value = None

    result = _invoke(runner, "bulk", ["--format", "json", "bulk", "eval", "--file", files["jobs"], "--wait"], client)

    assert result.exit_code == 1
    assert json.loads(result.stdout) == []
    assert "ev-1: disappeared while waiting" in _stderr(result)


# A partial failure exits 1 after printing what did succeed, so a script sees it.
def _two_jobs(files: Dict[str, str]) -> str:
    path = Path(files["jobs"])
    path.write_text('{"model": "gpt-4", "benchmark": "mmlu"}\n{"model": "no-such-model", "benchmark": "mmlu"}\n')
    return str(path)


def test_bulk_eval_from_a_file_exits_1_when_a_job_is_skipped(runner: CliRunner, files: Dict[str, str]) -> None:
    client = _client()
    client.models.get_by_key.side_effect = lambda key: MODEL if key == "gpt-4" else None
    client.models.get.return_value = []

    result = _invoke(runner, "bulk", ["--format", "json", "bulk", "eval", "--file", _two_jobs(files)], client)

    assert result.exit_code == 1
    assert [e["id"] for e in json.loads(result.stdout)] == ["ev-1"]
    assert "[2] SKIP" in _stderr(result)
    assert "1 of 2" in _stderr(result)


def test_bulk_eval_from_a_file_exits_1_when_a_job_fails(runner: CliRunner, files: Dict[str, str]) -> None:
    client = _client()
    client.evaluations.create.return_value = None

    result = _invoke(runner, "bulk", ["bulk", "eval", "--file", files["jobs"]], client)

    assert result.exit_code == 1
    assert "[1] FAIL" in _stderr(result)


def test_bulk_eval_from_a_file_exits_0_when_every_job_succeeds(runner: CliRunner, files: Dict[str, str]) -> None:
    result = _invoke(runner, "bulk", ["bulk", "eval", "--file", files["jobs"]])

    assert result.exit_code == 0, result.output + _stderr(result)


def test_bulk_eval_with_a_judge_exits_1_when_a_trace_evaluation_fails(runner: CliRunner, files: Dict[str, str]) -> None:
    client = _client()
    client.trace_evaluations.create.return_value = None

    result = _invoke(
        runner, "bulk", ["--format", "json", "bulk", "eval", "--judge-id", "j-1", "--traces", files["traces"]], client
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == []
    assert "[1] FAIL (trace=tr-1)" in _stderr(result)


@pytest.mark.parametrize("fmt", [[], ["--format", "json"]], ids=["table", "json"])
def test_a_failed_integration_test_exits_1(runner: CliRunner, fmt: List[str]) -> None:
    client = _client()
    client.integrations.test.return_value = api_models.TestIntegrationResponse(success=False, message="Unauthorized")

    result = _invoke(runner, "integration", [*fmt, "integration", "test", "i-1"], client)

    assert result.exit_code == 1
    assert "Integration i-1: FAILED" in result.stdout + _stderr(result)
    if fmt:
        assert json.loads(result.stdout) == {"success": False, "message": "Unauthorized"}


def test_a_passed_integration_test_exits_0(runner: CliRunner) -> None:
    result = _invoke(runner, "integration", ["integration", "test", "i-1"])

    assert result.exit_code == 0, result.output + _stderr(result)
    assert "Integration i-1: OK" in result.stdout


def test_bulk_eval_fails_when_its_evaluation_disappears_while_waiting(runner: CliRunner) -> None:
    client = _client()
    client.evaluations.wait_for_completion.return_value = None

    result = _invoke(
        runner,
        "bulk",
        ["--format", "json", "bulk", "eval", "--model", "gpt-4", "--benchmark", "mmlu", "--wait"],
        client,
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Evaluation disappeared while waiting." in _stderr(result)


def test_bulk_eval_with_a_judge_prints_the_created_trace_evaluations(runner: CliRunner, files: Dict[str, str]) -> None:
    result = _invoke(
        runner, "bulk", ["--format", "json", "bulk", "eval", "--judge-id", "j-1", "--traces", files["traces"]]
    )

    assert result.exit_code == 0, result.output + _stderr(result)
    assert [e["id"] for e in json.loads(result.stdout)] == ["te-1"]
    assert "[1] Created: te-1 (trace=tr-1)" in _stderr(result)


# (module, argv, client resource whose get_many comes back empty, the table's message)
EMPTY_LISTS = [
    ("judge", ["judge", "list"], "judges", "No judges found."),
    ("evaluate", ["evaluate", "list"], "evaluations", "No evaluations found."),
    ("integration", ["integration", "list"], "integrations", "No integrations found."),
    ("scorer", ["scorer", "list"], "scorers", "No scorers found."),
    ("trace", ["trace", "list"], "traces", "No traces found."),
    ("trace", ["trace", "search", "login"], "traces", "No traces found matching your query."),
]


def _empty_client(resource: str) -> Mock:
    client = _client()
    getattr(client, resource).get_many.return_value = SimpleNamespace(**{resource: []}, count=0, total_count=0)
    return client


@pytest.mark.parametrize("module, argv, resource, message", EMPTY_LISTS, ids=[" ".join(c[1]) for c in EMPTY_LISTS])
def test_an_empty_list_is_an_empty_json_list(
    runner: CliRunner, module: str, argv: List[str], resource: str, message: str
) -> None:
    result = _invoke(runner, module, ["--format", "json", *argv], _empty_client(resource))

    assert result.exit_code == 0, result.output + _stderr(result)
    assert json.loads(result.stdout) == []
    assert message in _stderr(result)


@pytest.mark.parametrize("module, argv, resource, message", EMPTY_LISTS, ids=[" ".join(c[1]) for c in EMPTY_LISTS])
def test_an_empty_list_in_a_table_still_says_so(
    runner: CliRunner, module: str, argv: List[str], resource: str, message: str
) -> None:
    result = _invoke(runner, module, argv, _empty_client(resource))

    assert result.exit_code == 0, result.output + _stderr(result)
    assert result.stdout.strip() == message


# (module, argv after `--format json`, a line stderr must hold): commands that return no data
RETURNS_NOTHING = [
    ("trace", ["trace", "delete", "tr-1", "--yes"], "Trace tr-1 deleted."),
    ("trace", ["trace", "export", "tr-1", "--output", "{out}"], "Trace exported to"),
    ("scorer", ["scorer", "delete", "s-1", "--yes"], "Scorer s-1 deleted."),
    ("scorer", ["scorer", "delete", "s-1", "--dry-run"], "[dry-run] Would delete scorer s-1"),
    (
        "scorer",
        [
            "scorer",
            "create",
            "--name",
            "Q",
            "--description",
            "Evaluate quality",
            "--model-id",
            "m-1",
            "--prompt",
            "Rate it",
            "--dry-run",
        ],
        "[dry-run] Would create scorer: Q",
    ),  # fmt: skip
    ("bulk", ["bulk", "eval", "--file", "{jobs}", "--dry-run"], "[dry-run] Would create 1 evaluation(s)"),
    ("bulk", ["bulk", "eval", "--model", "gpt-4", "--benchmark", "mmlu", "--dry-run"], "[dry-run] Would create"),
    ("bulk", ["bulk", "eval", "--judge-id", "j-1", "--traces", "{traces}", "--dry-run"], "trace=tr-1"),
    ("ci", ["ci", "report", "--latest", "1", "--output", "{out}"], "Report written to"),
    ("ci", ["ci", "report", "--evaluation-id", "ev-1", "--dry-run"], "[dry-run] Would report 1 benchmark evaluation"),
]


@pytest.mark.parametrize(
    "module, argv, status_line", RETURNS_NOTHING, ids=[" ".join(c[1][:3]) for c in RETURNS_NOTHING]
)
def test_a_command_without_data_leaves_stdout_empty(
    runner: CliRunner, files: Dict[str, str], module: str, argv: List[str], status_line: str
) -> None:
    argv = [arg.format(**files) for arg in argv]

    result = _invoke(runner, module, ["--format", "json", *argv])

    assert result.exit_code == 0, result.output + _stderr(result)
    assert result.stdout == ""
    assert status_line in _stderr(result)


def test_a_table_keeps_its_status_lines_on_stdout(runner: CliRunner) -> None:
    result = _invoke(runner, "judge", ["judge", "test", "--judge-id", "j-1", "--trace-id", "tr-1"])

    assert result.exit_code == 0, result.output + _stderr(result)
    assert "Trace evaluation created: te-1" in result.stdout


# The SDK returns None from get_many when it cannot read the response at all (empty,
# not an object, or the wrong shape). That is a failure, not "no results": printing
# `[]` (or "No judges found.") would tell a script the call worked and found nothing.
@pytest.mark.parametrize("fmt", [[], ["--format", "json"]], ids=["table", "json"])
@pytest.mark.parametrize("module, argv, resource, message", EMPTY_LISTS, ids=[" ".join(c[1]) for c in EMPTY_LISTS])
def test_an_unreadable_list_response_is_an_error(
    runner: CliRunner, fmt: List[str], module: str, argv: List[str], resource: str, message: str
) -> None:
    client = _client()
    getattr(client, resource).get_many.return_value = None

    result = _invoke(runner, module, [*fmt, *argv], client)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error:" in _stderr(result)
    assert message not in result.stdout + _stderr(result)


@pytest.mark.parametrize("module, argv, line", [
    ("trace", ["trace", "delete", "tr-1"], "Trace tr-1 deleted."),
    ("scorer", ["scorer", "delete", "s-1"], "Scorer s-1 deleted."),
])  # fmt: skip
def test_a_delete_confirmation_does_not_reach_stdout(
    runner: CliRunner, module: str, argv: List[str], line: str
) -> None:
    with patch(f"layerlens.cli.commands.{module}.get_client", return_value=_client()):
        result = runner.invoke(cli, ["--format", "json", *argv], input="y\n", env={"LAYERLENS_API_KEY": "k"})

    assert result.exit_code == 0, result.output + _stderr(result)
    assert "Are you sure" in _stderr(result)
    assert "Are you sure" not in result.stdout
    assert line in _stderr(result)


def test_synthetic_templates_as_json(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["synthetic", "templates", "--format", "json"])

    assert result.exit_code == 0, result.output + _stderr(result)
    assert json.loads(result.stdout)[0] == {"id": "llm.chat.basic", "category": "llm", "title": "LLM chat invocation"}


ORG = {"id": "org-1", "name": "Acme", "projects": [{"id": "proj-1", "name": "Default"}]}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """A fake LayerLens API at the httpx layer (the real client runs), with no stored login."""
    creds_dir = tmp_path / ".layerlens"
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_DIR", creds_dir)
    monkeypatch.setattr("layerlens.cli._auth.CREDENTIALS_FILE", creds_dir / "credentials")
    state = SimpleNamespace(traces_body={"status": "success", "data": {"traces": [], "count": 0, "total_count": 0}})

    def send(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if request.url.path.endswith("/organizations"):
            return httpx.Response(200, json={"data": ORG}, request=request)
        if request.url.path.endswith("/traces"):
            return httpx.Response(200, json=state.traces_body, request=request)
        return httpx.Response(404, json={"error": "unexpected"}, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    return state


def test_an_unreadable_response_from_the_api_is_an_error_not_an_empty_list(
    runner: CliRunner, api: SimpleNamespace
) -> None:
    api.traces_body = {}

    result = runner.invoke(cli, ["--format", "json", "trace", "list"], env={"LAYERLENS_API_KEY": "k"})

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error:" in _stderr(result)


def test_a_real_empty_list_from_the_api_is_an_empty_json_list(runner: CliRunner, api: SimpleNamespace) -> None:
    result = runner.invoke(cli, ["--format", "json", "trace", "list"], env={"LAYERLENS_API_KEY": "k"})

    assert result.exit_code == 0, result.output + _stderr(result)
    assert json.loads(result.stdout) == []


def test_whoami_with_an_api_key_as_json(runner: CliRunner, api: SimpleNamespace) -> None:
    result = runner.invoke(cli, ["whoami", "--format", "json"], env={"LAYERLENS_API_KEY": "k"})

    assert result.exit_code == 0, result.output + _stderr(result)
    assert json.loads(result.stdout) == {
        "auth": "api_key",
        "source": "LAYERLENS_API_KEY",
        "organization_id": "org-1",
        "project_id": "proj-1",
    }


def test_whoami_with_a_stored_login_as_json(runner: CliRunner, api: SimpleNamespace) -> None:
    from layerlens.cli._auth import save_credentials

    save_credentials(
        {
            "access_token": "access-tok",
            "id_token": "id-tok",
            "refresh_token": "refresh-tok",
            "expires_at": time.time() + 3600,
            "user": {"email": "someone@example.com", "name": "Someone", "sub": "user-1"},
        }
    )

    result = runner.invoke(cli, ["--format", "json", "whoami"])

    assert result.exit_code == 0, result.output + _stderr(result)
    assert json.loads(result.stdout) == {
        "auth": "login",
        "email": "someone@example.com",
        "name": "Someone",
        "id": "user-1",
    }
